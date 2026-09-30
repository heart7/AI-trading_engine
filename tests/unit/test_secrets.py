import logging

from engine.secrets.store import InMemorySecretStore, SecretRef, SecretValue


def test_secret_value_never_prints(caplog):
    s = SecretValue("super-secret-value-123")
    assert "super-secret" not in repr(s) and "super-secret" not in str(s) and "super-secret" not in f"{s}"
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("key=%s", s)
    assert "super-secret" not in caplog.text


def test_in_memory_roundtrip():
    st, ref = InMemorySecretStore(), SecretRef("venues/v1/read")
    st.put(ref, {"key": SecretValue("k"), "secret": SecretValue("s")})
    assert st.get(ref)["secret"].reveal() == "s"
    st.delete(ref)
