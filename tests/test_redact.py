"""The secret check, tested as one table: (text, expected kinds) per row.

Every row is a decision from the written contract. Values are assembled at
runtime so no real-looking credential sits in the source; all of them are made up.

The table is the specification of *what is detected*. It holds every example
the reviews of this feature raised, the accepted false positive
(``password: ask Jane``), each placeholder, and each shape at and just under its
minimum length. Rows that expect no kinds matter as much as the others: prose
without a separator, a placeholder, an ordinary URL, a URL fragment and a
differently cased copy of a provider prefix, query name or webhook host are not
credentials.

:func:`engagements.redact.secret_findings` also backs the doctor finding and the
write warning, so the structural tests here (area, section index, line,
occurrence, and the promise that a finding carries no record text) cover both.
The growth guards at the end compare four times the input with the time it
costs; they never assert an absolute time.
"""

import base64
import gc
import json
import time
from pathlib import Path

import pytest
from engagements import records, redact

REDACTED = redact.REDACTED
ROOT = Path(__file__).resolve().parent.parent

ASSIGNMENT = redact.ASSIGNMENT
PRIVATE_KEY = redact.PRIVATE_KEY
CLOUD_KEY = redact.CLOUD_ACCESS_KEY
API_KEY = redact.API_KEY
ACCESS_TOKEN = redact.ACCESS_TOKEN
BEARER_TOKEN = redact.BEARER_TOKEN
URL_CREDENTIALS = redact.URL_CREDENTIALS
WEBHOOK = redact.WEBHOOK_URL

# Made-up values, assembled so the source holds no credential-shaped literal.
Q = "Q7x9"
V8 = "Ab12" + "Cd34"                    # eight characters
LONG = "Zq9x7" + "Lm2Pw4" + "Rt6Yb8Nc0"
REVIEW = "Qv7Rt8" + "Lm2Np6" + "Za4Dc9"


def run(length, unit="Ab12"):
    """``length`` characters of a made-up token body."""
    return (unit * (length // len(unit) + 1))[:length]


def jwt_part(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
# A header of 5,384 characters: the first part used to be capped at 4,096.
LONG_JWT = (jwt_part({"alg": "HS256", "typ": "JWT", "audit": "A" * 4000}) + "."
            + jwt_part({"sub": "fictional"}) + "." + "Ab12Cd34" + "Ef56Gh78")
DOCS = "https://docs.example/"

# (text, the kinds the scan must report, in text order)
TABLE = [
    # --- assignments: every keyword, in every allowed spelling -----------------
    pytest.param(f"password: {Q}", [ASSIGNMENT], id="password"),
    pytest.param(f"passwd: {Q}", [ASSIGNMENT], id="passwd"),
    pytest.param(f"pwd: {Q}", [ASSIGNMENT], id="pwd"),
    pytest.param(f"passphrase: {Q}", [ASSIGNMENT], id="passphrase"),
    pytest.param(f"secret: {Q}", [ASSIGNMENT], id="secret"),
    pytest.param(f"client secret: {Q}", [ASSIGNMENT], id="client-secret-space"),
    pytest.param(f"client_secret: {Q}", [ASSIGNMENT], id="client-secret-underscore"),
    pytest.param(f"client-secret: {Q}", [ASSIGNMENT], id="client-secret-hyphen"),
    pytest.param(f"api key: {Q}", [ASSIGNMENT], id="api-key-space"),
    pytest.param(f"api_key: {Q}", [ASSIGNMENT], id="api-key-underscore"),
    pytest.param(f"api-key: {Q}", [ASSIGNMENT], id="api-key-hyphen"),
    pytest.param(f"apikey: {Q}", [ASSIGNMENT], id="apikey"),
    pytest.param(f"access key: {Q}", [ASSIGNMENT], id="access-key-space"),
    pytest.param(f"access_key: {Q}", [ASSIGNMENT], id="access-key-underscore"),
    pytest.param(f"access-key: {Q}", [ASSIGNMENT], id="access-key-hyphen"),
    pytest.param(f"secret key: {Q}", [ASSIGNMENT], id="secret-key-space"),
    pytest.param(f"secret_key: {Q}", [ASSIGNMENT], id="secret-key-underscore"),
    pytest.param(f"secret-key: {Q}", [ASSIGNMENT], id="secret-key-hyphen"),
    pytest.param(f"private key: {Q}", [ASSIGNMENT], id="private-key-space"),
    pytest.param(f"private_key: {Q}", [ASSIGNMENT], id="private-key-underscore"),
    pytest.param(f"private-key: {Q}", [ASSIGNMENT], id="private-key-hyphen"),
    pytest.param(f"token: {Q}", [ASSIGNMENT], id="token"),
    pytest.param(f"access token: {Q}", [ASSIGNMENT], id="access-token-space"),
    pytest.param(f"access_token: {V8}", [ASSIGNMENT], id="access-token-underscore"),
    pytest.param(f"access-token: {Q}", [ASSIGNMENT], id="access-token-hyphen"),
    pytest.param(f"auth token: {Q}", [ASSIGNMENT], id="auth-token-space"),
    pytest.param(f"auth_token: {V8}", [ASSIGNMENT], id="auth-token-underscore"),
    pytest.param(f"auth-token: {Q}", [ASSIGNMENT], id="auth-token-hyphen"),
    pytest.param(f"bearer: {Q}", [ASSIGNMENT], id="bearer-assignment"),
    # Keywords match case-insensitively; nothing else does.
    pytest.param(f"PASSWORD: {Q}", [ASSIGNMENT], id="keyword-upper-case"),
    pytest.param(f"Api-Key = {Q}", [ASSIGNMENT], id="keyword-mixed-case"),
    # Separators: ``:`` or ``=``, with optional spaces either side.
    pytest.param(f"password={Q}", [ASSIGNMENT], id="equals-no-spaces"),
    pytest.param(f"password = {Q}", [ASSIGNMENT], id="equals-spaces"),
    pytest.param(f"password :{Q}", [ASSIGNMENT], id="space-before-colon"),
    pytest.param(f"password  :  {Q}", [ASSIGNMENT], id="spaces-around-colon"),
    # A quoted keyword, in single or double quotes.
    pytest.param(f'"password": "{Q}"', [ASSIGNMENT], id="double-quoted-key"),
    pytest.param(f"'password': '{Q}'", [ASSIGNMENT], id="single-quoted-key"),
    pytest.param(f'{{"api_key": "{Q}"}}', [ASSIGNMENT], id="json-key"),
    pytest.param(f'{{"password": "{REVIEW}"}}', [ASSIGNMENT], id="review-json-body"),
    pytest.param(f"\"password': {Q}", [], id="mismatched-key-quotes"),
    # The keyword starts at a word boundary and is a whole keyword.
    pytest.param(f"notpassword: {V8}", [], id="no-word-boundary"),
    pytest.param(f"my_password: {Q}", [], id="underscore-is-a-word-character"),
    pytest.param(f"db-password: {Q}", [ASSIGNMENT], id="hyphen-is-a-boundary"),
    pytest.param(f"(password: {Q})", [ASSIGNMENT], id="parenthesis-is-a-boundary"),
    pytest.param(f"passwords: {Q}", [], id="keyword-plural"),
    pytest.param(f"password_hint: {Q}", [], id="keyword-prefix-of-a-word"),
    pytest.param(f"tokenizer: {Q}", [], id="keyword-inside-a-word"),
    pytest.param(f"secret_key: {Q}", [ASSIGNMENT], id="longest-keyword-is-one-finding"),
    # Any value at all: one character, one lowercase word, several words.
    pytest.param("pwd: 7", [ASSIGNMENT], id="one-character-value"),
    pytest.param("password: patience", [ASSIGNMENT], id="lowercase-word-value"),
    pytest.param("password: qvrtlmnpzadc", [ASSIGNMENT], id="lowercase-letters-value"),
    pytest.param("passphrase: violet river lantern", [ASSIGNMENT], id="multiword-value"),
    pytest.param('passphrase: "violet river lantern"', [ASSIGNMENT], id="quoted-multiword-value"),
    pytest.param('password: "stored in the vault"', [ASSIGNMENT], id="quoted-prose-value"),
    pytest.param("password: stored in the vault", [ASSIGNMENT], id="prose-value"),
    # The accepted false positive.
    pytest.param("password: ask Jane", [ASSIGNMENT], id="accepted-false-positive"),
    pytest.param(f"password: {REVIEW} # reference only", [ASSIGNMENT], id="trailing-comment"),
    pytest.param(f'password: "{REVIEW}" # reference only', [ASSIGNMENT], id="quoted-trailing-comment"),
    pytest.param("password: Qv7exampleRt8", [ASSIGNMENT], id="placeholder-word-inside-a-value"),
    pytest.param(f"password: {REVIEW}\nReference only: docs", [ASSIGNMENT], id="reference-on-next-line"),
    # The value ends where the next keyword and separator begin.
    pytest.param(f"password: {V8} token: Ef56Gh78", [ASSIGNMENT, ASSIGNMENT], id="two-assignments-one-line"),
    pytest.param(f"password: {REVIEW}; token: REDACTED", [ASSIGNMENT], id="value-then-placeholder"),
    pytest.param("token: REDACTED password: abc", [ASSIGNMENT], id="placeholder-then-value"),
    pytest.param("password: token: abc", [ASSIGNMENT], id="empty-value-before-next-key"),
    # Trimming leaves nothing: no value.
    pytest.param("password:", [], id="empty-value-at-end-of-text"),
    pytest.param("password:   ", [], id="spaces-only-value"),
    pytest.param('password: ""', [], id="empty-quoted-value"),
    pytest.param("password: ''", [], id="empty-single-quoted-value"),
    # An empty value takes the next line when it is indented further.
    pytest.param(f"password:\n  {V8}", [ASSIGNMENT], id="value-on-next-line"),
    pytest.param(f"password:\n\t{V8}", [ASSIGNMENT], id="value-on-tab-indented-line"),
    pytest.param(f"  password:\n    {V8}", [ASSIGNMENT], id="indented-key-deeper-value"),
    pytest.param(f"    password:\n  {V8}", [], id="next-line-less-indented"),
    pytest.param(f"  password:\n  {V8}", [], id="next-line-equally-indented"),
    pytest.param(f"password:\n{V8}", [], id="next-line-not-indented"),
    pytest.param(f'password:\n  "{V8}"', [ASSIGNMENT], id="quoted-value-on-next-line"),
    pytest.param("password:\n  REDACTED", [], id="placeholder-on-next-line"),
    pytest.param("password:\n  <paste here>", [], id="wrapped-placeholder-on-next-line"),
    pytest.param("password:\n     ", [], id="blank-next-line"),
    pytest.param(f"password: password:\n  {V8}", [ASSIGNMENT, ASSIGNMENT], id="two-empty-keys-share-next-line"),
    pytest.param(f"password:\n  token: {V8}", [ASSIGNMENT, ASSIGNMENT], id="next-line-holds-its-own-key"),
    pytest.param(f"password: {V8}\n  {Q}", [ASSIGNMENT], id="filled-value-ignores-next-line"),
    # --- prose without a separator is never a finding -------------------------
    pytest.param("reset her password on Monday", [], id="prose-password"),
    pytest.param("Credentials: password manager collection Harbor", [], id="prose-credentials"),
    pytest.param("The token is Expired", [], id="is-is-not-a-separator"),
    pytest.param(f"password is {Q}", [], id="password-is-value"),
    pytest.param("Reference: see the password manager", [], id="reference-prose"),
    pytest.param("Rotate the API key monthly", [], id="api-key-prose"),
    # --- placeholders, each of them --------------------------------------------
    pytest.param("password: <value>", [], id="placeholder-angle"),
    pytest.param("token: <paste token here>", [], id="placeholder-angle-sentence"),
    pytest.param("password: [redacted]", [], id="placeholder-square"),
    pytest.param("password: {{ vault.portal }}", [], id="placeholder-brace"),
    pytest.param("password: <>", [], id="placeholder-empty-wrapper"),
    pytest.param("password: ********", [], id="placeholder-stars"),
    pytest.param("password: xxxx", [], id="placeholder-x-run"),
    pytest.param("password: XXXX", [], id="placeholder-x-run-upper"),
    pytest.param("password: ....", [], id="placeholder-dots"),
    pytest.param("password: \u2022\u2022\u2022\u2022", [], id="placeholder-bullets"),
    pytest.param("password: x.*X\u2022", [], id="placeholder-mixed-run"),
    pytest.param("password: redacted", [], id="placeholder-word-redacted"),
    pytest.param("password: REDACTED", [], id="placeholder-word-redacted-upper"),
    pytest.param("password: none", [], id="placeholder-word-none"),
    pytest.param("password: None", [], id="placeholder-word-none-title"),
    pytest.param("password: n/a", [], id="placeholder-word-na"),
    pytest.param("password: N/A", [], id="placeholder-word-na-upper"),
    pytest.param("password: tbd", [], id="placeholder-word-tbd"),
    pytest.param("password: TBD", [], id="placeholder-word-tbd-upper"),
    pytest.param("password: changeme", [], id="placeholder-word-changeme"),
    pytest.param("password: ChangeMe", [], id="placeholder-word-changeme-mixed"),
    pytest.param("password: example", [], id="placeholder-word-example"),
    pytest.param("password: Example", [], id="placeholder-word-example-title"),
    pytest.param("password: 'redacted'", [], id="placeholder-word-quoted"),
    # Not placeholders: a mismatched wrapper, a run with another character, a
    # placeholder word with more text.
    pytest.param("password: <value]", [ASSIGNMENT], id="mismatched-wrapper"),
    pytest.param("password: [value}", [ASSIGNMENT], id="mismatched-square-wrapper"),
    pytest.param("password: <", [ASSIGNMENT], id="lone-angle"),
    pytest.param("password: xxxx-xxxx", [ASSIGNMENT], id="run-with-hyphen"),
    pytest.param("password: xxxy", [ASSIGNMENT], id="run-with-letter"),
    pytest.param("password: examples", [ASSIGNMENT], id="placeholder-word-plural"),
    pytest.param("password: none yet", [ASSIGNMENT], id="placeholder-word-and-more"),
    # --- shapes: private key ----------------------------------------------------
    pytest.param(PEM, [PRIVATE_KEY], id="pem-rsa"),
    pytest.param("-----BEGIN " + "PRIVATE KEY-----", [PRIVATE_KEY], id="pem-no-label"),
    pytest.param("-----BEGIN " + "OPENSSH PRIVATE KEY-----", [PRIVATE_KEY], id="pem-openssh"),
    pytest.param("-----BEGIN " + "ENCRYPTED PRIVATE KEY-----", [PRIVATE_KEY], id="pem-encrypted"),
    pytest.param("key file: " + PEM + " (copied)", [PRIVATE_KEY], id="pem-mid-line"),
    pytest.param("-----begin " + "RSA PRIVATE KEY-----", [], id="pem-lower-case"),
    pytest.param("-----BEGIN " + "rsa private key-----", [], id="pem-label-lower-case"),
    pytest.param("-----BEGIN " + "PUBLIC KEY-----", [], id="pem-public-key"),
    pytest.param("-----END " + "RSA PRIVATE KEY-----", [], id="pem-end-marker"),
    # --- shapes: cloud access key -------------------------------------------------
    pytest.param("AKIA" + run(16, "AB12"), [CLOUD_KEY], id="akia-16"),
    pytest.param("AKIA" + run(15, "AB12"), [], id="akia-15"),
    pytest.param("ASIA" + run(16, "AB12"), [CLOUD_KEY], id="asia-16"),
    pytest.param("ASIA" + run(15, "AB12"), [], id="asia-15"),
    pytest.param("key AKIA" + run(40, "AB12"), [CLOUD_KEY], id="akia-long"),
    pytest.param("akia" + run(16, "AB12"), [], id="akia-lower-case"),
    pytest.param("AKIA" + run(16, "ab12"), [], id="akia-lower-case-body"),
    # --- shapes: API key ---------------------------------------------------------
    pytest.param("sk-" + run(16), [API_KEY], id="sk-16"),
    pytest.param("sk-" + run(15), [], id="sk-15"),
    pytest.param("sk_live_" + run(16), [API_KEY], id="sk-live-16"),
    pytest.param("sk_live_" + run(15), [], id="sk-live-15"),
    pytest.param("sk_test_" + run(16), [API_KEY], id="sk-test-16"),
    pytest.param("sk_test_" + run(15), [], id="sk-test-15"),
    pytest.param("rk_live_" + run(16), [API_KEY], id="rk-live-16"),
    pytest.param("rk_live_" + run(15), [], id="rk-live-15"),
    pytest.param("pk_live_" + run(16), [API_KEY], id="pk-live-16"),
    pytest.param("pk_live_" + run(15), [], id="pk-live-15"),
    pytest.param("SK-" + run(16), [], id="sk-upper-case"),
    pytest.param("SK_LIVE_" + run(16), [], id="sk-live-upper-case"),
    pytest.param("AIza" + run(35), [API_KEY], id="aiza-35"),
    pytest.param("AIza" + run(34), [], id="aiza-34"),
    pytest.param("aiza" + run(35), [], id="aiza-lower-case"),
    pytest.param("AIza" + REVIEW + REVIEW[:17], [API_KEY], id="review-aiza-heading"),
    pytest.param(("aiza" + REVIEW + REVIEW[:17]).lower(), [], id="review-aiza-casefolded"),
    # --- shapes: access token ------------------------------------------------------
    pytest.param("ghp_" + run(20), [ACCESS_TOKEN], id="ghp-20"),
    pytest.param("ghp_" + run(19), [], id="ghp-19"),
    pytest.param("gho_" + run(20), [ACCESS_TOKEN], id="gho-20"),
    pytest.param("gho_" + run(19), [], id="gho-19"),
    pytest.param("ghs_" + run(20), [ACCESS_TOKEN], id="ghs-20"),
    pytest.param("ghs_" + run(19), [], id="ghs-19"),
    pytest.param("github_pat_" + run(20), [ACCESS_TOKEN], id="github-pat-20"),
    pytest.param("github_pat_" + run(19), [], id="github-pat-19"),
    pytest.param("GHP_" + run(20), [], id="ghp-upper-case"),
    pytest.param("xoxb-" + run(10, "1234"), [ACCESS_TOKEN], id="xoxb-10"),
    pytest.param("xoxb-" + run(9, "1234"), [], id="xoxb-9"),
    pytest.param("xoxp-" + run(10, "1234"), [ACCESS_TOKEN], id="xoxp-10"),
    pytest.param("xoxp-" + run(9, "1234"), [], id="xoxp-9"),
    pytest.param("xoxa-" + run(10, "1234"), [ACCESS_TOKEN], id="xoxa-10"),
    pytest.param("xoxa-" + run(9, "1234"), [], id="xoxa-9"),
    pytest.param("xoxr-" + run(10, "1234"), [ACCESS_TOKEN], id="xoxr-10"),
    pytest.param("xoxs-" + run(10, "1234"), [ACCESS_TOKEN], id="xoxs-10"),
    pytest.param("XOXB-" + run(10), [], id="xoxb-upper-case"),
    pytest.param("eyJ" + "A.B.C", [ACCESS_TOKEN], id="jwt-shortest-parts"),
    pytest.param("eyJ" + ".B.C", [ACCESS_TOKEN], id="jwt-first-part-is-the-prefix"),
    pytest.param("x" + "eyJ" + "a.b.c", [ACCESS_TOKEN], id="jwt-inside-a-run"),
    pytest.param("eyJ" + "a.b.c.d", [ACCESS_TOKEN], id="jwt-four-parts-is-one"),
    pytest.param(LONG_JWT, [ACCESS_TOKEN], id="jwt-long-header"),
    pytest.param("eyJ" + "a.b", [], id="jwt-two-parts"),
    pytest.param("eyJ" + "a..c", [], id="jwt-empty-middle-part"),
    pytest.param("eyJ" + "a.b.", [], id="jwt-empty-last-part"),
    pytest.param("eyj" + "Ab12.Cd34.Ef56", [], id="jwt-lower-case-prefix"),
    pytest.param("EYJ" + "Ab12.Cd34.Ef56", [], id="jwt-upper-case-prefix"),
    # --- shapes: bearer token ------------------------------------------------------
    pytest.param("Bearer " + run(16), [BEARER_TOKEN], id="bearer-16"),
    pytest.param("Bearer " + run(15), [], id="bearer-15"),
    pytest.param("Bearer\t" + run(16), [BEARER_TOKEN], id="bearer-tab"),
    pytest.param("Authorization: Bearer " + run(16), [BEARER_TOKEN], id="bearer-header"),
    pytest.param("Bearer " + REVIEW, [BEARER_TOKEN], id="review-bearer-heading"),
    pytest.param("bearer " + REVIEW, [], id="review-bearer-lower-case"),
    pytest.param("BEARER " + run(16), [], id="bearer-upper-case"),
    pytest.param("Bearer" + run(16), [], id="bearer-no-space"),
    # --- shapes: URLs ---------------------------------------------------------------
    pytest.param("https://acme:" + LONG + "@files.example/share", [URL_CREDENTIALS], id="url-password"),
    pytest.param("https://user:p@docs.example/", [URL_CREDENTIALS], id="url-password-one-character"),
    pytest.param("ftp://acme:" + Q + "@files.example/", [URL_CREDENTIALS], id="url-password-other-scheme"),
    pytest.param("https://user:***@docs.example/", [], id="url-password-placeholder-stars"),
    pytest.param("https://user:<password>@docs.example/", [], id="url-password-placeholder-wrapped"),
    pytest.param("https://user@docs.example/", [], id="url-user-only"),
    pytest.param("https://user:@docs.example/", [], id="url-empty-password"),
    # A query parameter named like an assignment keyword is also an assignment:
    # ``token=`` is a keyword and a separator wherever it appears.
    pytest.param(DOCS + "?token=" + V8, [URL_CREDENTIALS, ASSIGNMENT], id="query-token"),
    pytest.param(DOCS + "?key=" + V8, [URL_CREDENTIALS], id="query-key"),
    pytest.param(DOCS + "?api_key=" + V8, [URL_CREDENTIALS, ASSIGNMENT], id="query-api-key"),
    pytest.param(DOCS + "?apikey=" + V8, [URL_CREDENTIALS, ASSIGNMENT], id="query-apikey"),
    pytest.param(DOCS + "?secret=" + V8, [URL_CREDENTIALS, ASSIGNMENT], id="query-secret"),
    pytest.param(DOCS + "?signature=" + V8, [URL_CREDENTIALS], id="query-signature"),
    pytest.param(DOCS + "?sig=" + V8, [URL_CREDENTIALS], id="query-sig"),
    pytest.param(DOCS + "?sig=" + V8[:7], [], id="query-seven-characters"),
    pytest.param(DOCS + "?signature=xxxxxxxx", [], id="query-placeholder-run"),
    pytest.param(DOCS + "?token=<api-token>", [], id="query-placeholder-wrapped"),
    pytest.param(DOCS + "?SIGNATURE=" + V8, [], id="query-name-upper-case"),
    pytest.param(DOCS + "?Token=" + V8, [ASSIGNMENT], id="query-name-title-case-is-only-an-assignment"),
    pytest.param(DOCS + "?page=" + V8, [], id="query-other-name"),
    pytest.param(DOCS + "?a=1&sig=" + V8, [URL_CREDENTIALS], id="query-second-parameter"),
    pytest.param(DOCS + "page?sig=" + V8 + "#top", [URL_CREDENTIALS], id="query-before-fragment"),
    pytest.param("https://docs.example/page#?signature=" + V8, [], id="fragment-is-not-a-query"),
    pytest.param("https://docs.example/page?x=1#sig=" + V8, [], id="parameter-in-fragment"),
    pytest.param("https://hooks.slack.com/services/T000/B000/" + LONG, [WEBHOOK], id="webhook-slack"),
    pytest.param("https://hooks.slack.com/services/", [WEBHOOK], id="webhook-slack-bare-path"),
    pytest.param("https://discord.com/api/webhooks/1/" + LONG, [WEBHOOK], id="webhook-discord"),
    pytest.param("https://discordapp.com/api/webhooks/1/" + LONG, [WEBHOOK], id="webhook-discordapp"),
    pytest.param("https://acme.webhook.office.com/webhookb2/" + LONG, [WEBHOOK], id="webhook-office"),
    pytest.param("https://audit.webhook.office.com", [WEBHOOK], id="webhook-office-bare-host"),
    pytest.param("https://user:" + Q + "@hooks.slack.com/services/x", [WEBHOOK], id="webhook-with-password-is-one"),
    pytest.param("https://hooks.slack.com/apps/" + LONG, [], id="slack-other-path"),
    pytest.param("https://discord.com/api/other/" + LONG, [], id="discord-other-path"),
    pytest.param("https://webhook.office.com/" + LONG, [], id="office-host-without-subdomain"),
    pytest.param("https://HOOKS.SLACK.COM/services/" + V8, [], id="webhook-host-upper-case"),
    pytest.param("https://Discord.com/api/webhooks/1/" + V8, [], id="webhook-host-title-case"),
    pytest.param("https://hooks.example/services/" + REVIEW, [], id="hooks-host-elsewhere"),
    pytest.param("https://hooks.example/services/" + REVIEW.lower(), [], id="hooks-host-casefolded"),
    pytest.param("https://hooks.example/services/" + REVIEW + "_SuffixProbe9", [], id="hooks-host-suffix"),
    pytest.param("https://hooks.example/documentation-2026", [], id="hooks-example-docs"),
    pytest.param("https://books.example/fishing-hook-guide-2026", [], id="books-example"),
    pytest.param("https://hooks.example/DocumentationGuide", [], id="mixed-case-path"),
    pytest.param("https://docs.example/Path/MixedCase/" + REVIEW, [], id="mixed-case-path-token"),
    pytest.param("https://files.example/share?page=2", [], id="ordinary-url"),
    # Boundaries of the private-key header and the JWT walk.
    pytest.param("-----BEGIN " + "RSA PRIVATE KEY----", [], id="pem-four-trailing-dashes"),
    pytest.param("version 1.2.3", [], id="dotted-runs-without-eyj"),
    pytest.param("eyk1.b.c", [], id="jwt-prefix-other-letter"),
    pytest.param("eyJ" + "a b c", [], id="jwt-parts-split-by-spaces"),
    pytest.param("eyJ" + "a,b,c", [], id="jwt-parts-split-by-commas"),
    pytest.param("eyJ" + "a.b c", [], id="jwt-third-part-after-a-space"),
    pytest.param("x " + "eyJ" + "a.b.c", [ACCESS_TOKEN], id="jwt-after-a-word"),
    pytest.param("eyJ" + "a.b.c " + "eyJ" + "d.e.f", [ACCESS_TOKEN, ACCESS_TOKEN], id="two-jwts-one-line"),
    pytest.param("eyJ" + "a.b." + "eyJ" + "c.d.e", [ACCESS_TOKEN], id="jwt-walk-resumes-after-a-match"),
    # Boundaries of URL parsing.
    pytest.param("https://hooks.slack.com:443/services/" + V8, [WEBHOOK], id="webhook-with-port"),
    pytest.param("https://hooks.slack.com/servicesX/" + V8, [], id="slack-path-prefix-without-slash"),
    pytest.param("https://discord.com/api/webhooksX/" + V8, [], id="discord-path-prefix-without-slash"),
    pytest.param("https://discordapp.com/api/webhooksX/" + V8, [], id="discordapp-path-prefix-without-slash"),
    pytest.param("https://discordapp.com/other/" + V8, [], id="discordapp-other-path"),
    pytest.param("https://docs.example?sig=" + V8, [URL_CREDENTIALS], id="query-without-a-path"),
    pytest.param("file:///notes?sig=" + V8, [URL_CREDENTIALS], id="query-after-an-empty-authority"),
    pytest.param("see ://user:" + Q + "@files.example/", [], id="no-scheme-is-not-a-url"),
    pytest.param("://x https://user:" + Q + "@files.example/", [URL_CREDENTIALS], id="url-after-a-bare-separator"),
    pytest.param("https:// user:" + Q + "@files.example/", [], id="space-after-the-separator"),
    pytest.param("file:///user:" + Q + "@files.example", [], id="password-shape-in-a-path"),
    pytest.param("9://user:" + Q + "@files.example/", [], id="scheme-without-a-letter"),
    # --- several findings on one line, in text order --------------------------------
    pytest.param("password: sk-" + run(16), [ASSIGNMENT, API_KEY], id="assignment-holding-a-shape"),
    pytest.param("token=ghp_" + run(20), [ASSIGNMENT, ACCESS_TOKEN], id="token-holding-a-shape"),
    pytest.param(DOCS + "?token=ghp_" + run(20), [URL_CREDENTIALS, ASSIGNMENT, ACCESS_TOKEN],
                 id="url-holding-an-assignment-and-a-shape"),
    pytest.param("sk-" + run(16) + " sk-" + run(17), [API_KEY, API_KEY], id="two-shapes-one-line"),
]


@pytest.mark.parametrize(("text", "kinds"), TABLE)
def test_the_table(text, kinds):
    assert [finding["kind"] for finding in redact.secret_findings(text)] == kinds


def test_every_kind_appears_in_the_table():
    kinds = {kind for param in TABLE for kind in param.values[1]}
    assert kinds == set(redact.KINDS)


def test_the_shipped_fixtures_and_demo_carry_no_findings():
    """The plugin ships example records; none of them looks like a credential."""
    checked = 0
    for base in ("tests/fixtures", "demo"):
        for path in sorted((ROOT / base).rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            assert redact.secret_findings(text) == [], path.relative_to(ROOT).as_posix()
            checked += 1
    assert checked > 10


# --- the placeholder constant -----------------------------------------------------


def test_the_placeholders_are_one_constant():
    assert redact.PLACEHOLDERS["words"] == {"redacted", "none", "n/a", "tbd", "changeme", "example"}
    assert redact.PLACEHOLDERS["wrappers"] == {("<", ">"), ("[", "]"), ("{", "}")}
    assert redact.PLACEHOLDERS["run"] == set("*xX\u2022.")


@pytest.mark.parametrize("value", ["", "  ", "<a>", "[a]", "{a}", "*", "x", "X", "\u2022", ".",
                                   "xX*.\u2022", "redacted", "NONE", "N/a", "Tbd", "CHANGEME", "eXample",
                                   ' "tbd" ', "'<a>'"])
def test_a_placeholder(value):
    assert redact.is_placeholder(value)


@pytest.mark.parametrize("value", ["a", "<a]", "(a)", "xy", "x-x", "redact", "examples", "n/a!"])
def test_not_a_placeholder(value):
    assert not redact.is_placeholder(value)


# --- structure: area, section, line, occurrence -----------------------------------

RECORD = "\n".join([
    "---",                                   # 1
    "engagements: client",                   # 2
    f"api_key: {Q}",                         # 3   property
    "---",                                   # 4
    f"token: {Q}",                           # 5   body (before any section)
    "## Contacts",                           # 6   section 1
    f"password: {Q} token: {V8}",            # 7   two findings on one line
    "```",                                   # 8   a fence: its heading is text
    "## Not a heading",                      # 9
    f"secret: {Q}",                          # 10  still section 1
    "```",                                   # 11
    "# Level one ends the section",          # 12
    f"pwd: {Q}",                             # 13  body
    "## Change  Log ##",                     # 14  section 2, the change log
    f"- 2026-01-01 studio: created (source: token: {Q})",  # 15
    "## Heading #not-closing",               # 16  section 3
    f"passwd: {Q}",                          # 17
])


def test_a_finding_names_area_section_line_and_occurrence_only():
    assert redact.secret_findings(RECORD) == [
        {"line": 3, "area": "property", "section": None, "kind": ASSIGNMENT, "occurrence": 1},
        {"line": 5, "area": "body", "section": None, "kind": ASSIGNMENT, "occurrence": 1},
        {"line": 7, "area": "section", "section": 1, "kind": ASSIGNMENT, "occurrence": 1},
        {"line": 7, "area": "section", "section": 1, "kind": ASSIGNMENT, "occurrence": 2},
        {"line": 10, "area": "section", "section": 1, "kind": ASSIGNMENT, "occurrence": 1},
        {"line": 13, "area": "body", "section": None, "kind": ASSIGNMENT, "occurrence": 1},
        {"line": 15, "area": "change_log", "section": 2, "kind": ASSIGNMENT, "occurrence": 1},
        {"line": 17, "area": "section", "section": 3, "kind": ASSIGNMENT, "occurrence": 1},
    ]


def test_crlf_and_a_byte_order_mark_keep_the_layout():
    text = "\ufeff" + RECORD.replace("\n", "\r\n")
    assert redact.secret_findings(text) == redact.secret_findings(RECORD)


def test_without_frontmatter_everything_is_body():
    text = f"## Contacts\npassword: {Q}"
    assert redact.secret_findings(text) == [
        {"line": 2, "area": "body", "section": None, "kind": ASSIGNMENT, "occurrence": 1}]


def test_a_value_on_the_next_line_is_located_on_that_line():
    text = f"---\nid: a\n---\n## Contacts\npassword:\n    {V8}"
    assert redact.secret_findings(text) == [
        {"line": 6, "area": "section", "section": 1, "kind": ASSIGNMENT, "occurrence": 1}]


def test_occurrences_count_every_finding_on_a_line_in_order():
    text = f"password: sk-{run(16)} token: {V8}"
    found = redact.secret_findings(text)
    assert [(item["kind"], item["occurrence"]) for item in found] == [
        (ASSIGNMENT, 1), (API_KEY, 2), (ASSIGNMENT, 3)]


@pytest.mark.parametrize(("heading", "area"), [
    ("## Change log", "change_log"),
    ("## change LOG", "change_log"),
    ("##   Change log   ", "change_log"),
    ("## Change log ##", "change_log"),
    ("## Change log #", "change_log"),
    ("## Change log##", "section"),
    ("   ## Change log", "change_log"),
    ("    ## Change log", None),
    ("##Change log", None),
    ("### Change log", None),
    ("## Change logs", "section"),
])
def test_the_change_log_heading_follows_the_parser(heading, area):
    text = f"---\nid: a\n---\n## First\n{heading}\npassword: {Q}"
    (finding,) = redact.secret_findings(text)
    if area is None:
        assert (finding["area"], finding["section"]) == ("section", 1)
    else:
        assert (finding["area"], finding["section"]) == (area, 2)


@pytest.mark.parametrize(("lines", "area", "section"), [
    # Frontmatter only when the text starts with it and it closes.
    pytest.param(["intro", "---", "## A", f"password: {Q}"], "body", None, id="dashes-later-are-not-frontmatter"),
    pytest.param(["---", f"password: {Q}"], "body", None, id="unclosed-frontmatter-is-body"),
    pytest.param(["---", "---", "## A", f"password: {Q}"], "section", 1, id="empty-frontmatter"),
    # Fences: a run of three or more, indented at most three spaces.
    pytest.param(["---", "---", "## A", "``", "## B", f"password: {Q}"], "section", 2,
                 id="two-backticks-are-not-a-fence"),
    pytest.param(["---", "---", "## A", "`", "## B", f"password: {Q}"], "section", 2,
                 id="one-backtick-is-not-a-fence"),
    pytest.param(["---", "---", "## A", "   ```", "## B", "```", f"password: {Q}"], "section", 1,
                 id="fence-indented-three-spaces"),
    pytest.param(["---", "---", "## A", "    ```", "## B", f"password: {Q}"], "section", 2,
                 id="four-spaces-is-not-a-fence"),
    pytest.param(["---", "---", "## A", "```text", "## B", "```", f"password: {Q}"], "section", 1,
                 id="fence-with-an-info-string"),
    pytest.param(["---", "---", "## A", "```a`b", "## B", f"password: {Q}"], "section", 2,
                 id="backtick-info-string-is-not-a-fence"),
    pytest.param(["---", "---", "## A", "~~~a`b", "## B", "~~~", f"password: {Q}"], "section", 1,
                 id="tilde-fence-allows-a-backtick"),
    pytest.param(["---", "---", "## A", "```", "~~~", "## B", "```", f"password: {Q}"], "section", 1,
                 id="other-marker-does-not-close"),
    pytest.param(["---", "---", "## A", "````", "```", "## B", "````", f"password: {Q}"], "section", 1,
                 id="shorter-run-does-not-close"),
    pytest.param(["---", "---", "## A", "```", "```text", "## B", "```", f"password: {Q}"], "section", 1,
                 id="closing-run-takes-no-info-string"),
    pytest.param(["---", "---", "## A", "```", "```\t", "## B", f"password: {Q}"], "section", 2,
                 id="closing-run-allows-trailing-tab"),
    # Headings: whitespace after the hashes can be a tab, and so can trailing space.
    pytest.param(["---", "---", "## A", "##\tChange log", f"password: {Q}"], "change_log", 2, id="tab-after-hashes"),
    pytest.param(["---", "---", "## A", "##  \tChange log\t", f"password: {Q}"], "change_log", 2,
                 id="tabs-around-the-title"),
    pytest.param(["---", "---", "## A", "## Change log\t##", f"password: {Q}"], "change_log", 2,
                 id="closing-hashes-after-a-tab"),
    pytest.param(["---", "---", "## A", "## Change log\t## \t", f"password: {Q}"], "change_log", 2,
                 id="closing-hashes-then-trailing-whitespace"),
])
def test_the_layout_follows_the_parser(lines, area, section):
    (finding,) = redact.secret_findings("\n".join(lines))
    assert (finding["area"], finding["section"]) == (area, section)


def test_the_layout_agrees_with_the_parser_on_every_shipped_record():
    """Section numbers come from the detector's own walk; the parser must agree."""
    checked = 0
    for base in ("tests/fixtures", "demo"):
        for path in sorted((ROOT / base).rglob("*.md")):
            data = path.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # the parser reads no sections from it; doctor reports the encoding
            record = records.parse(data)
            lines, _offsets = redact._lines(data.decode("utf-8", "replace"))
            _areas, numbers = redact._layout(lines)
            starts, position = [], 0
            for raw in data.split(b"\n"):
                starts.append(position)
                position += len(raw) + 1
            expected = [None] * len(lines)
            for number, section in enumerate(record.sections, start=1):
                for index, start in enumerate(starts):
                    if section.start <= start < section.end:
                        expected[index] = number
            # The empty element after a final newline is not a line of the file.
            real = len(lines) - data.endswith(b"\n")
            assert numbers[:real] == expected[:real], path.relative_to(ROOT).as_posix()
            checked += bool(record.sections)
    assert checked > 10


def test_a_finding_never_holds_text_from_the_record():
    found = redact.secret_findings(RECORD)
    dumped = json.dumps(found)
    for line in RECORD.split("\n"):
        if len(line) > 3:
            assert line not in dumped
    assert Q not in dumped and V8 not in dumped
    for item in found:
        assert set(item) == {"line", "area", "section", "kind", "occurrence"}
        assert item["kind"] in redact.KINDS
        assert item["area"] in redact.AREAS


def test_location_text_names_only_the_structure():
    assert redact.location_text("property", None, 4) == "line 4 of the property area"
    assert redact.location_text("section", 2, 13) == "line 13 of section 2"
    assert redact.location_text("change_log", 3, 16) == "line 16 of the change log (section 3)"
    assert redact.location_text("body", None, 7) == "line 7 of the body"
    assert redact.location_text("section", 2, 13, 2) == "line 13 of section 2, finding 2 on that line"


# --- update comparison: a set of (kind, value), never a position -------------------


@pytest.mark.parametrize(("before", "after", "new"), [
    pytest.param(f"password: {V8}", f"password: {V8}", 0, id="unchanged"),
    pytest.param(f"password: {V8}", f"x\n\npassword: {V8}", 0, id="moved"),
    pytest.param(f"password: {V8}", f"password: {V8}\npassword: {V8}", 0, id="copied"),
    pytest.param(f"password: {V8}", f"password: {V8}x", 1, id="changed"),
    pytest.param(f"password: {V8}", f"password: {V8.lower()}", 1, id="changed-case-only"),
    pytest.param("", f"password: {V8}", 1, id="added"),
    pytest.param(f"password: {V8}", "", 0, id="removed"),
    pytest.param(f'password: "{V8}"', f"password: {V8}", 0, id="quotes-removed"),
    pytest.param(f"password: {V8}", f"password: '{V8}'", 0, id="quotes-added"),
    pytest.param(f"password: {V8}", f'password:\n  "{V8}"', 0, id="moved-to-next-line-quoted"),
    pytest.param(f"password:\n    {V8}", f"token: {V8}", 0, id="other-keyword-same-kind-and-value"),
    pytest.param("sk-" + run(16), "password: sk-" + run(16), 1, id="same-value-new-kind"),
    pytest.param(f"https://docs.example/?sig={V8}", f"https://docs.example/?sig={V8}&a=1", 1,
                 id="url-changed"),
])
def test_new_findings_compare_kind_and_trimmed_value(before, after, new):
    assert len(redact.new_findings(before, after)) == new


def test_new_findings_keep_the_structure_of_the_new_record():
    before = f"---\nid: a\n---\n## A\npassword: {V8}"
    after = f"---\nid: a\n---\n## A\npassword: {V8} token: {Q}"
    assert redact.new_findings(before, after) == [
        {"line": 5, "area": "section", "section": 1, "kind": ASSIGNMENT, "occurrence": 2}]


# --- the redaction older diagnostics use ---------------------------------------------


@pytest.mark.parametrize(("text", "kept"), [
    (f"Duplicate section: 'password: {LONG}'.", "Duplicate section: '"),
    (f"Record defect on 'sk-{run(16)}'.", "Record defect on '"),
    ("note " + PEM + " tail", "note "),
])
def test_a_diagnostic_keeps_its_words_and_loses_the_value(text, kept):
    redacted = redact.redact_text(text)
    assert redacted.startswith(kept)
    assert REDACTED in redacted
    assert LONG not in redacted and run(16) not in redacted


def test_a_long_value_is_redacted_to_its_end():
    redacted = redact.redact_text("password: " + "Q" * 5000 + "PrivateTailMarker")
    assert redacted == "password: " + REDACTED


def test_a_quoted_value_with_an_inner_quote_is_redacted_whole():
    redacted = redact.redact_text("password: ab'cd'ef")
    assert redacted == "password: " + REDACTED


def test_a_value_on_the_next_line_is_redacted_whole():
    redacted = redact.redact_text(f"password:\n    {LONG}")
    assert redacted == "password:\n    " + REDACTED


def test_a_private_key_block_is_redacted_to_its_end_marker():
    block = PEM + "\nMIIEowIBAAKCAQEA\n" + "-----END " + "RSA PRIVATE KEY-----"
    assert redact.redact_text("key: see below\n" + block + "\nafter") == "key: see below\n" + REDACTED + "\nafter"
    assert redact.redact_text(PEM + "\nMIIEowIBAAKCAQEA") == REDACTED


def test_redaction_matches_shapes_in_any_case_and_detection_does_not():
    label = "sk-" + run(16).lower()
    assert redact.secret_findings(label.upper()) == []
    assert redact.redact_label(label.upper()) == REDACTED
    assert redact.looks_secret(label.upper())
    assert redact.redact_label("Contacts") == "Contacts"
    assert not redact.looks_secret(None)


def test_a_placeholder_is_not_redacted():
    assert redact.redact_text("password: <value>") == "password: <value>"


def test_redaction_masks_exactly_the_value_with_tabs_around_it():
    assert redact.redact_text(f"password:\t{LONG}\t") == "password:\t" + REDACTED + "\t"
    assert redact.redact_text(f"password:\n\t{LONG}\t") == "password:\n\t" + REDACTED + "\t"


@pytest.mark.parametrize(("text", "expected"), [
    pytest.param("https://user:" + Q + "@files.example/", REDACTED, id="url-at-the-start"),
    pytest.param("see https://user:" + Q + "@files.example/ now", "see " + REDACTED + " now", id="url-after-a-word"),
    pytest.param("see Zx-y+z.w://user:" + Q + "@files.example/", "see " + REDACTED, id="every-scheme-character"),
    pytest.param("x 9https://user:" + Q + "@files.example/", "x 9" + REDACTED, id="scheme-starts-at-a-letter"),
    pytest.param("eyJ" + "a.b.c tail", REDACTED + " tail", id="jwt-to-its-third-part"),
    pytest.param("EYJ" + "a.b.c", REDACTED, id="jwt-prefix-in-any-case"),
    pytest.param("version 1.2.3", "version 1.2.3", id="dotted-runs-are-not-a-jwt"),
    pytest.param("eyk1.b.c", "eyk1.b.c", id="jwt-prefix-other-letter"),
    pytest.param("a:/bcd://user:" + Q + "@files.example/", "a:/" + REDACTED, id="scheme-stops-at-a-slash"),
    pytest.param("https://a.example/ z:/bcd://user:" + Q + "@files.example/",
                 "https://a.example/ z:/" + REDACTED, id="second-url-scheme-stops-at-a-slash"),
    pytest.param("x:// a:/bcd://user:" + Q + "@files.example/", "x:// a:/" + REDACTED,
                 id="scheme-after-a-bare-separator-stops-at-a-slash"),
])
def test_redaction_masks_a_url_or_token_from_its_first_character(text, expected):
    assert redact.redact_text(text) == expected


def test_a_lower_case_private_key_block_is_redacted_to_its_end_marker():
    text = "-----begin rsa private key-----\nMIIE\n-----end rsa private key-----\nafter"
    assert redact.redact_text(text) == REDACTED + "\nafter"


def test_private_key_blocks_pair_each_header_with_the_next_end_marker():
    end = "-----END " + "RSA PRIVATE KEY-----"
    text = "\n".join([PEM, "MIIE", end, "middle", PEM, "MIIF", end, "after"])
    assert redact.redact_text(text) == "\n".join([REDACTED, "middle", REDACTED, "after"])
    assert redact.redact_text(PEM + end + " tail") == REDACTED + " tail"
    assert redact.redact_text("intro " + end + "\n" + PEM + "\nMIIE") == "intro " + end + "\n" + REDACTED


def test_an_end_marker_sharing_the_header_dashes_does_not_close_it():
    """A block closes at the first end marker that starts after its header ends."""
    shared = "-----BEGIN " + "RSA PRIVATE KEY" + "-----END RSA PRIVATE KEY-----"
    assert redact.redact_text(shared + "\ntail") == REDACTED


def test_a_four_dash_header_is_not_a_block():
    text = "-----BEGIN " + "RSA PRIVATE KEY----\nnot a key"
    assert redact.redact_text(text) == text


@pytest.mark.parametrize(("spans", "merged"), [
    ([(0, 2), (2, 4)], [(0, 4)]),
    ([(0, 4), (1, 2)], [(0, 4)]),
    ([(0, 2), (1, 5), (7, 9)], [(0, 5), (7, 9)]),
    ([(3, 4), (0, 1)], [(0, 1), (3, 4)]),
    ([(0, 2), (5, 10), (6, 8)], [(0, 2), (5, 10)]),
])
def test_spans_are_merged_into_their_union(spans, merged):
    assert redact._merged(spans) == merged


def test_a_non_string_diagnostic_passes_through():
    assert redact.redact_text(None) is None
    assert redact.redact_label(None) is None
    assert redact.redact_text(42) == 42


# --- growth: four times the input costs less than eight times the time ---------------
#
# Each guard compares the best of three runs on an input with four times as many
# characters. There is no absolute bound: the assertion is about the shape of the
# cost, so a slow or busy machine does not decide it.

SMALL = 100_000
LARGE = 4 * SMALL


def _repeated(unit, size):
    return (unit * (size // len(unit) + 1))[:size]


def _best_seconds(function, argument, attempts=3):
    best = float("inf")
    for _attempt in range(attempts):
        gc.collect()  # so no sample pays for garbage left by the one before it
        began = time.perf_counter()
        function(argument)
        best = min(best, time.perf_counter() - began)
    return best


def _empty_keys_then(value_character):
    def build(size):
        return "password: " * (size // 20) + "\n " + value_character * (size // 2)
    return build


GROWTH_INPUTS = [
    pytest.param(lambda size: _repeated(f"password: {Q} ", size), id="repeated-assignments"),
    pytest.param(lambda size: _repeated(f'password: "{Q}" ', size), id="repeated-quoted-assignments"),
    pytest.param(lambda size: _repeated("password    ", size), id="whitespace-near-matches"),
    pytest.param(lambda size: f"password: {Q}" + " " * size + "Z", id="whitespace-after-a-value"),
    pytest.param(lambda size: _repeated("password:" + " " * 16, size), id="empty-assignments"),
    pytest.param(lambda size: _repeated("https://docs.example/path/", size), id="near-miss-urls"),
    pytest.param(lambda size: _repeated("https://hooks.example/abcdefghijklmnop/ ", size), id="hook-near-misses"),
    pytest.param(lambda size: _repeated("a", size) + "://", id="long-scheme-run"),
    pytest.param(lambda size: _repeated("a://", size), id="repeated-scheme-separators"),
    pytest.param(_empty_keys_then("q"), id="empty-keys-share-a-long-next-line"),
    pytest.param(_empty_keys_then("x"), id="empty-keys-share-a-placeholder-next-line"),
    pytest.param(lambda size: "---\nid: a\n---\n## Heading " + " \t" * (size // 2) + "q",
                 id="heading-with-inner-whitespace"),
    pytest.param(lambda size: "---\nid: a\n---\n" + _repeated("## Heading \t #\n", size), id="many-headings"),
    pytest.param(lambda size: _repeated("eyJ", size), id="jwt-prefix-run"),
    pytest.param(lambda size: _repeated("eyJa.", size), id="jwt-dotted-run"),
    pytest.param(lambda size: "eyJ" + _repeated("a", size) + ".b.c", id="jwt-long-first-part"),
    pytest.param(lambda size: _repeated("Bearer ", size), id="repeated-bearer"),
    pytest.param(lambda size: _repeated("AKIA", size), id="repeated-akia"),
    pytest.param(lambda size: _repeated(PEM + " ", size), id="repeated-pem-headers"),
    pytest.param(lambda size: _repeated("-----BEGIN ", size), id="pem-near-misses"),
]


@pytest.mark.parametrize("build", GROWTH_INPUTS)
def test_the_scan_grows_linearly(build):
    small = _best_seconds(redact.secret_findings, build(SMALL))
    large = _best_seconds(redact.secret_findings, build(LARGE))
    assert large < small * 8, f"{SMALL} characters {small:.4f}s, {LARGE} characters {large:.4f}s"


@pytest.mark.parametrize("build", [
    pytest.param(lambda size: _repeated(PEM + "\n", size), id="unmatched-pem-begin-markers"),
    pytest.param(lambda size: _repeated(f"password: {Q} ", size), id="repeated-assignments"),
    pytest.param(_empty_keys_then("q"), id="empty-keys-share-a-long-next-line"),
])
def test_the_diagnostic_redaction_grows_linearly(build):
    small = _best_seconds(redact.redact_text, build(SMALL))
    large = _best_seconds(redact.redact_text, build(LARGE))
    assert large < small * 8, f"{SMALL} characters {small:.4f}s, {LARGE} characters {large:.4f}s"


def test_many_findings_are_counted_in_one_pass_and_grow_linearly():
    small = _best_seconds(redact.secret_findings, "\n".join(f"password: {V8}{n}" for n in range(10_000)))
    large = _best_seconds(redact.secret_findings, "\n".join(f"password: {V8}{n}" for n in range(40_000)))
    assert large < small * 8, f"10,000 findings {small:.4f}s, 40,000 findings {large:.4f}s"
