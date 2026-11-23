import base64
import struct

import pytest

from scripts.authorize_notebook_key import remote_script, validate_key


def public_key():
    wire = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
    return "ssh-ed25519 " + base64.b64encode(wire).decode() + " example-notebook"


def test_public_key_normalized_and_fingerprinted_without_comment():
    key, fingerprint = validate_key(public_key())
    assert "example-notebook" not in key
    assert fingerprint.startswith("SHA256:")
    assert validate_key("ssh-ed25519 " + key)[1] == fingerprint


@pytest.mark.parametrize("text", ["", "PRIVATE KEY", "ssh-rsa AAAA", "ssh-ed25519 !!!!", "ssh-ed25519 AAAA", public_key()+"\n"+public_key(), "command=anything "+public_key()])
def test_private_malformed_or_option_bearing_keys_are_rejected(text):
    with pytest.raises(ValueError):
        validate_key(text)


def test_preview_is_read_only_and_generated_code_is_valid_python():
    key, fingerprint = validate_key(public_key())
    script = remote_script(key, fingerprint, "second-notebook", False)
    code = script.split("\n", 1)[1].rsplit("\nDRIVEMODS_ENROLLMENT", 1)[0]
    compile(code, "remote-enrollment", "exec")
    assert '"apply": false' in code
    assert "systemctl" not in code and "sshd_config" not in code


def test_shell_injection_and_mismatched_fingerprint_are_rejected():
    key, fingerprint = validate_key(public_key())
    with pytest.raises(ValueError):
        remote_script(key, fingerprint, "x; command", True)
    with pytest.raises(ValueError):
        remote_script(key, "incorrect", "notebook", True)
