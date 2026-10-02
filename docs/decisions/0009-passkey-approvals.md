# 0009 — Approvals with a passkey instead of a USB security key

Status: requested by the principal on 2026-10-02 ("a different form of authentication that doesn't require a USB
stick"). Supersedes the "hardware-key touch" wording of spec §0.4 item 1, INV-33 and §16.11. Everything else in
§0.4 stands: single signer, written rationale inside the signed bytes, email and push notice per approval, cooling-off
fixed at 0h.

1. **Default method.** A passkey (WebAuthn) on the principal's phone or laptop, unlocked with Face ID, Touch ID,
   Windows Hello or the device PIN. The approval page `docs/passkey/index.html` creates the passkey and signs approval
   statements; its output is a complete approval record. No command line is needed.
2. **What every approval proves.** The challenge is SHA-256 of the canonical statement (action, subject hash, signer,
   rationale, time, namespace). The engine checks the enrolled credential, the `webauthn.get` type, the challenge, the
   enrolled origin and RP ID, and that the authenticator set both user-presence and user-verification flags. A
   passkey without a fresh biometric or device PIN is refused (`REAUTH_REQUIRED`).
3. **Backup.** Enrol a second passkey on another device, or rely on a synced passkey (iCloud Keychain, Google
   Password Manager), which is restored with the account. Synced passkeys report a counter of 0; the replay check
   still applies to non-zero counters.
4. **Hardware keys still work.** A FIDO2 key enrolled through OpenSSH (`sk-` keys) stays valid, so the principal can
   add one later without code changes.
5. **Trade-off.** A USB key cannot be copied; a synced passkey is only as safe as the Apple or Google account that
   syncs it. Mitigations: that account must have its own strong sign-in, every approval still needs a fresh
   biometric or PIN, and every approval sends an email and push notice, so a forged approval would be seen.
6. **Where the page runs.** WebAuthn needs HTTPS (or `http://localhost`). The intended host is GitHub Pages for this
   repository, giving the RP ID `heart7.github.io`. A passkey is bound to the site that created it, so enrol and sign
   on the same address.
