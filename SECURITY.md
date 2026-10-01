# Security policy

DailyViral is an automation client that handles Instagram credentials and R2
object credentials. Making its source public does not require publishing any of
those values. This document defines what the repository enforces and what the
operator must configure separately.

## Threat model

| Threat | Repository controls | Operator responsibility |
| --- | --- | --- |
| Secrets committed accidentally | Broad runtime-file exclusions, publication-file checks, Gitleaks scans of history and current files | Enable GitHub secret scanning/push protection; inspect old refs, forks, releases, and artifacts |
| Pull request steals production credentials | Public CI has no application-secret references; publishing has only schedule/manual triggers and a default-branch gate | Store credentials only in a protected `publishing` environment; restrict deployment branches and reviewed merges |
| Dependency/action replacement | Full action commit pins, exact runtime Python versions with hashes, pinned CI tools, vulnerability audits, Dependabot | Review updates; protect workflow/config files and maintainers' accounts |
| Public R2 endpoint exposes objects | Private S3 operations; managed/custom domain checks before reads/uploads; legacy public configuration rejected | Disable public domains; remove or secure Workers/Pages routes and old caches |
| Signed URL disclosed | GET-only, one object, 5–30 minute bounds, log redaction, GitHub masking, immediate cleanup | Treat signed URLs as bearer credentials; do not store them in public issues or artifacts |
| Runner crashes with generated object remaining | Random per-attempt object keys, cleanup on handled outcomes | Set a `generated/` lifecycle expiry rule and inspect failed runs |
| Token leaks through HTTP errors | Official Meta origin allowlist, redirect refusal, header auth where supported, no response-body/error-URL echo | Use valid scoped tokens; rotate credentials and review external telemetry |
| Video URL probes a private network | HTTPS-only, public-IP checks on every redirect, connections pinned to the checked IP with hostname TLS verification | Only supply trusted source media; keep dependencies and runners updated |
| Malformed media exhausts resources | Download limits, FFmpeg protocol restrictions and timeout, workflow timeout | Use trusted MP4 assets; monitor storage costs and job failures |

Cloud publishing gives Meta temporary access to one rendered object. That is
necessary for Instagram content publishing. A signed URL can be used by **anyone
who possesses it** until expiration or deletion. It is not bound exclusively to
Meta. It grants no bucket-listing or upload permission. If even that temporary
read is unacceptable, keep `AUTO_POST_ENABLED=false` and `DRY_RUN=true`.

## Before making the repository public

### Repository and history

- Run CI locally and on GitHub. Scan every reachable Git ref with
  `gitleaks git --redact --log-opts="--all" .`, plus current publication files.
- Confirm `.env`, actual account configuration, local databases, logs, generated
  videos, private keys, and editor/session files are untracked. The application
  still keeps these files locally; never upload the entire working directory.
- Review historical commits, remote branches/tags, commit messages, releases,
  Actions logs/artifacts/caches, issues, and pull-request attachments for secrets
  and private information. A clean local history scan does not cover all of these.
- Rotate any credential ever exposed. Removing a secret from the latest file or
  rewriting history cannot invalidate copies of it. Rewrite affected history only
  after revocation, and coordinate with collaborators if required.
- Delete stale public artifacts and logs that contain signed URLs, token
  fragments, or sensitive media. The new redaction code cannot change old logs.

### Cloudflare account

- Disable the bucket's r2.dev URL and remove public custom domains. Confirm
  direct anonymous object requests are denied, including old domain routes.
- Review all Workers/Pages bindings and routes that reference the bucket. The
  automated domain verifier cannot discover an independently deployed proxy.
- Purge cached copies formerly served through public domains. Previously
  downloaded or published media cannot be made secret retroactively.
- Use a dedicated media bucket and bucket-scoped Object Read & Write credentials.
  Do not use an admin write credential in the application.
- The separate `Workers R2 Storage Read` verification token can read data across
  the selected account. Prefer a dedicated account if other private data exists;
  the verifier does not need write or token-management privileges.
- Remove unnecessary credentials and account members, use multifactor
  authentication, and rotate credentials when exposure is suspected.
- Configure expiration for `generated/` objects; keep the source under `base/`.
- Check the credential scopes in the dashboard. Prefix restrictions in Python
  prevent accidental operations by this code, but are not an authorization policy
  for someone who has stolen a bucket-wide credential.

### GitHub repository

- Create the `publishing` environment and restrict it to the default branch.
  Move secrets there and remove repository/organization copies accessible to
  untrusted jobs in this repository.
- Require CI and code-owner approval on the default branch. Protect changes to
  workflows, dependencies, application code, and code ownership. Disallow bypass
  where feasible.
- Enable secret scanning, push protection, Dependabot alerts, read-only workflow
  permissions, and outside-contributor workflow approval.
- Use hosted, ephemeral runners. Do not add untrusted pull-request jobs to a
  persistent runner that holds deployment credentials.
- Decide whether publishing requires an environment reviewer. Approval protects
  each credential-bearing run but pauses unattended scheduled publication.
- Protect maintainer accounts with multifactor authentication and audit installed
  GitHub Apps and deploy keys. A trusted maintainer can change code that receives
  secrets; a workflow cannot protect against unrestricted trusted-admin access.

### First deployment

- Upload the source privately; remove `R2_PUBLIC_BASE_URL` and the cloud pipeline's
  old public source-video URL configuration.
- Run the read-only schedule readiness command from a trusted local environment.
  Confirm the private source exists, public domains are disabled, and Meta accepts
  the token. Do not paste its environment into a public issue.
- Start with preview and duplicate protection enabled. Enable actual publishing
  only after reviewing readiness.
- Confirm a real post and object deletion. Exercise the failure path without
  credentials in a test environment and monitor the lifecycle backup.

## Repository audit scope

The local audit performed during this hardening inspected five reachable commits
and 46 historical file blobs. Gitleaks reported no findings. The local credential
value checked separately was absent from reachable historical blobs, and no
`.env`, private account YAML, database, or private-key file was found in that
history. These are findings about this checkout, not a guarantee that a credential
was never exposed elsewhere.

Offline tests and dependency audits do not prove live Cloudflare privacy, token
scopes, environment protections, successful Instagram fetches, or absence of
previously cached media. The hardened flow intentionally requires new private-R2
configuration and may stop an older public-bucket deployment until migration.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** / private vulnerability reporting feature
when the repository owner has enabled it. If that feature is unavailable, open an
issue requesting a private contact channel without including exploit details or
credentials. Do not post access tokens, signed URLs, account configuration, video
URLs, or logs containing secrets in a public issue.

Provide the affected version, reproduction steps using dummy values, impact,
and a proposed mitigation. There is no promised response-time SLA; this is a
personal project. Security fixes target the current default branch.

## If exposure is suspected

1. Disable publishing and revoke or rotate affected Instagram, R2, and Cloudflare
   API credentials immediately. Invalidate access first; deleting a commit is
   insufficient.
2. Disable public bucket paths and independently deployed proxies. Remove
   temporary objects and purge domain caches if applicable.
3. Review GitHub Actions runs, repository changes, Cloudflare audit records,
   account members, tokens, and object operations for unauthorized activity.
4. Remove leaked data from history and distribution points after revocation.
   Assume forks or prior downloads may retain copies.
5. Restore only scoped credentials and trusted configuration, run validation,
   then enable publishing deliberately.

## Boundaries

This project does not claim formal verification, penetration-testing certification,
or complete prevention of credential theft. Third-party vulnerabilities, compromised
maintainers, misconfigured cloud accounts, machine compromise, and copied public
media remain possible. Hashes establish artifact integrity against the reviewed
lock; they do not establish that a dependency is harmless. Scanners detect known
patterns and advisories; clean results are useful evidence with limited scope.
