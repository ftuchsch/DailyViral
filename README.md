# DailyViral

DailyViral is a local Python CLI for queueing and publishing Instagram Reels to Instagram Creator or Business accounts through the official Meta Instagram Graph API.

It is built for a manual media workflow:

1. You upload the video somewhere public yourself.
2. You paste the direct public video URL into the queue.
3. DailyViral validates the URL, prepares the caption, and publishes through Meta.

## What It Does

- Stores a post queue in SQLite.
- Supports multiple Instagram accounts in config, starting cleanly with one.
- Validates that a video URL is public and looks like a direct video file.
- Supports manual captions or a simple `{day_number}` caption template.
- Publishes Reels through the official Meta Graph API flow.
- Records success and failure outcomes, Instagram media IDs, and permalinks.
- Supports dry-run mode and confirmation prompts for manual safety.

## What It Does Not Do

- It does not use browser automation, scraping, unofficial APIs, or Instagram cookies.
- It does not upload or host videos for you.
- It does not generate captions with an LLM yet.
- It does not provide a public web UI or SaaS layer.
- The `analytics` command is local queue analytics, not Instagram insights reporting.

## Requirements

- Python 3.10+
- An Instagram Creator or Business account connected properly through Meta
- A Meta app with Instagram content publishing access
- A public direct video URL that Meta can fetch without login or cookies

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Project Layout

```text
dailyviral/
  __init__.py
  __main__.py
  analytics.py
  captions.py
  cli.py
  config.py
  instagram.py
  queue.py
  url_validation.py
config/
  accounts.example.yaml
data/
  new_post.example.json
  posts.sqlite
logs/
.env.example
README.md
requirements.txt
```

## Initialization

Run:

```bash
python -m dailyviral init
```

This will:

- create `config/`, `data/`, and `logs/` if missing
- create `config/accounts.yaml` from `config/accounts.example.yaml` if missing
- create `logs/dailyviral.log`
- initialize `data/posts.sqlite`

## Meta Setup

Use only the official Instagram Platform / Graph API. Start from the official docs:

- Instagram Platform docs: `https://developers.facebook.com/docs/instagram-platform/`
- Content publishing docs: `https://developers.facebook.com/docs/instagram-platform/content-publishing/`

At a high level you need:

1. A Meta app with Instagram Platform access.
2. An Instagram professional account: Creator or Business.
3. The Instagram account connected properly in Meta.
4. Permissions needed for publishing, typically including `instagram_content_publish`.
5. A valid access token for the Instagram account you want to post to.

Before trying to publish, verify in Meta that:

- the Instagram account is connected correctly
- the token is valid and not expired
- your app has the permissions required for content publishing

## Configuration

Copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

Important environment variables:

- `AUTO_POST_ENABLED`
  - `false` means `run-due` and `post-next` run in dry-run mode only
  - `true` allows real publishing
- `CONFIRM_BEFORE_POSTING`
  - `true` asks for confirmation before posting
- `DRY_RUN`
  - if `true`, force dry-run mode even if auto-posting is enabled
- `GRAPH_API_BASE_URL`
  - defaults to `https://graph.instagram.com`
- `GRAPH_API_VERSION`
  - defaults to `v23.0`; keep this aligned with the Meta Graph API version you use
- `INSTAGRAM_USERNAME`
  - your Instagram professional account username
- `INSTAGRAM_ACCESS_TOKEN`
  - your local access token; never commit it
- `DAILYVIRAL_VIDEO_URL`
  - a direct public video URL for the next local test post

Configure accounts in `config/accounts.yaml`:

```yaml
accounts:
  - account_name: main_meme_account
    instagram_username: your_instagram_username
    ig_user_id: "17841400000000000"
    access_token_env_var: INSTAGRAM_ACCESS_TOKEN
    default_caption_template: "Day {day_number} of posting this meme"
    default_hashtags: "#meme #daily"
    required_hashtags: "#meme #daily #coach #fails #GetEmRightCoach #YouGoodCoach"
    timezone: America/Los_Angeles
```

Notes:

- `instagram_username` should match your target Instagram account.
- `ig_user_id` is the Instagram user ID used by the Graph API.
- `access_token_env_var` points to the environment variable name, not the token itself.
- `default_caption_template` is optional.
- `default_hashtags` is optional.
- `required_hashtags` are always appended if they are missing.
- `timezone` controls which posts are considered due on a given day for that account.

## Acceptable Video URLs

DailyViral expects a URL that Meta can fetch server-to-server.

Good examples:

- a public `https://.../video.mp4`
- a public object-storage file URL
- a CDN URL that returns the video file directly

Common bad examples:

- Google Drive preview/share pages
- Dropbox preview pages
- HTML landing pages
- URLs that only work with browser cookies or login

Validation checks:

- URL is public `http` or `https`
- server responds successfully
- redirects are followed
- response looks like a direct video file
- HTML preview pages are rejected

MP4 is the safest target format for this MVP.

## Queueing Posts

Interactive mode:

```bash
python -m dailyviral add
```

You can also provide values directly:

```bash
python -m dailyviral add \
  --account-name main_meme_account \
  --video-url https://example.com/meme.mp4 \
  --caption "Day 17 of posting this meme" \
  --hashtags "#meme #daily" \
  --scheduled-date 2026-05-13
```

JSON file mode:

```bash
python -m dailyviral add-from-file data/new_post.example.json
```

If `caption` is blank and the account has a template, DailyViral fills it automatically using the next day number.

Queue directly from your local `.env` video URL:

```bash
python -m dailyviral add-real-post main_meme_account
```

## Posting One Reel

Config and dry-run sequence:

```bash
python -m dailyviral check-config main_meme_account
python -m dailyviral init
python -m dailyviral add-real-post main_meme_account
python -m dailyviral status
python -m dailyviral dry-run main_meme_account
```

`check-config` will:

- load `.env` through `python-dotenv`
- verify required env vars exist
- verify the selected account exists in `config/accounts.yaml`
- verify the configured username, IG user ID, and access token env var
- validate that `DAILYVIRAL_VIDEO_URL` is publicly reachable
- print a pass/fail report without printing the full token

Dry run command:

```bash
python -m dailyviral dry-run main_meme_account
```

Dry run will:

- validate the video URL
- prepare the final caption
- show account name, username, IG user ID, video URL, and final caption
- show `AUTO_POST_ENABLED` and `DRY_RUN`
- avoid publishing anything
- leave the post queued

Force dry run explicitly:

```bash
python -m dailyviral post-next main_meme_account --dry-run
```

Real publish:

Change `.env` to:

```bash
AUTO_POST_ENABLED=true
DRY_RUN=false
```

Then run:

```bash
python -m dailyviral post-next main_meme_account --yes
```

The publish flow is:

1. pick the next due queued post
2. validate the public video URL
3. check Instagram content publishing limits if available
4. create a Reel media container
5. poll until processing finishes
6. publish the media container
7. fetch and store the permalink if available

If `AUTO_POST_ENABLED=false` or `DRY_RUN=true`, `post-next` will not publish and will fall back to dry-run behavior instead.

## Schedule 30 Days Ahead

Step 1:
Make sure `.env` has:

```bash
AUTO_POST_ENABLED=true
DRY_RUN=false
```

Step 2:
Queue the next 30 days:

```bash
python -m dailyviral schedule-days main_meme_account --days 30
```

Step 3:
Inspect upcoming posts:

```bash
python -m dailyviral upcoming main_meme_account
```

Step 4:
Test the daily runner safely by temporarily setting:

```bash
AUTO_POST_ENABLED=false
DRY_RUN=true
```

Then run:

```bash
python -m dailyviral run-due main_meme_account --yes
```

Step 5:
Turn real posting back on:

```bash
AUTO_POST_ENABLED=true
DRY_RUN=false
```

Step 6:
Install daily scheduling on macOS:

```bash
python -m dailyviral make-launchd main_meme_account --time "09:00" --install
```

Step 7:
Verify launchd is loaded and logs are written to:

```text
logs/launchd.out.log
logs/launchd.err.log
```

The Mac must be awake and connected to the internet when the scheduled job runs.

## Status And Analytics

Queue overview:

```bash
python -m dailyviral status
python -m dailyviral status --account-name main_meme_account
```

Upcoming queue:

```bash
python -m dailyviral upcoming main_meme_account
```

Recent posted posts:

```bash
python -m dailyviral posted main_meme_account
```

Local queue analytics:

```bash
python -m dailyviral analytics
python -m dailyviral analytics --account-name main_meme_account
```

## Logging

Runtime logs are written to:

```text
logs/dailyviral.log
```

Each publish attempt is logged with:

- account name
- video URL
- caption
- validation result
- API outcome
- permalink or failure reason

## Scheduling Later

Cron example:

```bash
python -m dailyviral make-cron main_meme_account --time "09:00"
```

macOS launchd generation:

```bash
python -m dailyviral make-launchd main_meme_account --time "09:00"
```

The daily runner command is:

```bash
python -m dailyviral run-due main_meme_account --yes
```

GitHub Actions later:

- check out the repo
- install Python dependencies
- provide `.env` values through GitHub Actions secrets
- run `python -m dailyviral run-due main_meme_account --yes`

## Warnings

- Use only content you have rights to post.
- Respect Meta platform rules and Instagram publishing limits.
- Do not rely on preview URLs or private storage links.
- Creator/Business account setup and app permissions must be correct before real publishing will work.
