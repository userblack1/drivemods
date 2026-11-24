import hashlib,json,subprocess,zipfile
from pathlib import Path
import pytest
from scripts.build_crypto_release import REQUIRED,build


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],capture_output=True,check=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path,'init');git(tmp_path,'config','user.name','Release test')
    git(tmp_path,'config','user.email','release@example.invalid')
    for name in REQUIRED:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('# reviewed placeholder\n',encoding='utf-8')
    (tmp_path/'.gitignore').write_text('/release/\n/data/\n*.env\n')
    git(tmp_path,'add','.');git(tmp_path,'commit','-m','Reviewed test release')
    return tmp_path


def test_archive_is_complete_deterministic_and_contains_no_private_working_files(repo):
    private=repo/'data';private.mkdir();(private/'secret.env').write_text('Not a source file')
    first=build(repo,repo/'release/first.zip');second=build(repo,repo/'release/second.zip')
    assert first.read_bytes()==second.read_bytes()
    with zipfile.ZipFile(first) as z:
        names=z.namelist()
        assert 'DriveMods-checkout/crypto_checkout/application.py' in names
        assert not any('/data/' in n or '/.git/' in n or n.endswith('secret.env') for n in names)
        manifest=json.loads(z.read('DriveMods-checkout/release-manifest.json'))
        assert manifest['history_included'] is False
        for name,digest in manifest['files'].items():
            assert hashlib.sha256(z.read('DriveMods-checkout/'+name)).hexdigest()==digest


@pytest.mark.parametrize('staged',[False,True])
def test_dirty_source_cannot_be_mislabeled_as_committed_release(repo,staged):
    (repo/'README.md').write_text('unreviewed change')
    if staged:git(repo,'add','README.md')
    with pytest.raises(ValueError):build(repo,repo/'release/source.zip')
    assert not (repo/'release/source.zip').exists()


def test_destination_cannot_escape_release_or_replace_existing_archive(repo):
    with pytest.raises(ValueError):build(repo,repo/'README.zip')
    first=build(repo,repo/'release/source.zip');original=first.read_bytes()
    with pytest.raises(ValueError):build(repo,first)
    assert first.read_bytes()==original


def test_tracked_private_history_prevents_release_even_if_current_file_removed(repo):
    p=repo/'data/private.txt';p.parent.mkdir();p.write_text('Private operational state')
    git(repo,'add','-f','data/private.txt');git(repo,'commit','-m','Accidental private state')
    git(repo,'rm','data/private.txt');git(repo,'commit','-m','Remove private state')
    with pytest.raises(ValueError):build(repo,repo/'release/source.zip')
