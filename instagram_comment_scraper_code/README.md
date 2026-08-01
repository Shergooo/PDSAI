# Instagram Comment Scraper

A production-ready Selenium scraper for extracting Instagram post comments with engagement metrics. Features resumable storage, session persistence, and structured JSON output.

## Features

- **Resumable scraping** - SQLite database tracks progress, skip already-completed posts
- **Session persistence** - Cookie storage avoids repeated logins
- **Engagement metrics** - Extracts likes and replies for each comment
- **Flexible output** - JSON files + SQLite database for easy data access
- **Proxy support** - Route through named proxies from a config file
- **Manual verification handling** - Waits for you to complete Instagram challenges

## Installation

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
python3 -m pip install -r requirements.txt
```

**Requirements:** Chrome browser must be installed. Selenium Manager automatically downloads the matching ChromeDriver.

## Quick Start

1. Create a text file with Instagram post URLs (one per line):
   ```
   https://www.instagram.com/p/ABC123/
   https://www.instagram.com/p/DEF456/
   ```

2. Set your Instagram credentials as environment variables:
   ```bash
   export INSTAGRAM_USERNAME="your_username"
   export INSTAGRAM_PASSWORD="your_password"
   ```

3. Run the scraper:
   ```bash
   python3 instagram_comment_scraper.py --urls urls.txt
   ```

## Output Files

- **JSON files:** `outputs/instagram_posts/<shortcode>.json` - Individual post data
- **Database:** `outputs/instagram_scrape.sqlite3` - All posts and comments
- **Session:** `work/instagram_session_cookies.json` - Login session persistence

## Output Format

Each post is saved as a JSON file with this structure:

```json
{
  "url": "https://www.instagram.com/p/xxxxxx/",
  "post_timestamp": "2024-01-15T10:30:00.000Z",
  "post_publisher_is_verified": true,
  "post_total_likes": 15234,
  "post_total_comments_count": 45,
  "comments": [
    {
      "comment": "Great post! 🔥",
      "likes": 234,
      "replies": 12
    },
    {
      "comment": "This is amazing content!",
      "likes": 89,
      "replies": 5
    }
  ]
}
```

## Command-Line Options

### Basic Usage

```bash
python3 instagram_comment_scraper.py --urls urls.txt
```

### Recommended Options

```bash
python3 instagram_comment_scraper.py \
  --urls urls.txt \
  --expand-replies \
  --skip-complete
```

- `--expand-replies` - Click "View all replies" buttons to load threaded comments
- `--skip-complete` - Skip posts already successfully scraped in the database

### Advanced Options

```bash
python3 instagram_comment_scraper.py \
  --urls urls.txt \
  --output-dir outputs/instagram_posts \
  --db outputs/instagram_scrape.sqlite3 \
  --cookie-file work/instagram_session_cookies.json \
  --headless \
  --max-scrolls 250 \
  --stagnation-limit 8
```

- `--headless` - Run Chrome in headless mode (test without this first)
- `--max-scrolls` - Maximum scroll attempts per post (default: 250)
- `--stagnation-limit` - Stop after N unchanged scroll rounds (default: 8)

## Rate Limiting

The scraper uses human-like delays by default. For even slower pacing:

```bash
python3 instagram_comment_scraper.py \
  --urls urls.txt \
  --login-min-delay 8 \
  --login-max-delay 16 \
  --post-min-delay 20 \
  --post-max-delay 45 \
  --manual-challenge-wait 600
```

- `--login-min-delay` / `--login-max-delay` - Pause range around login actions
- `--post-min-delay` / `--post-max-delay` - Pause range between posts
- `--manual-challenge-wait` - Seconds to wait for manual verification (default: 300)

## Proxy Configuration

Create a JSON file (e.g., `proxies.json`) with your proxy details:

```json
{
  "proxy1": "http://username:password@ip:port",
  "proxy2": "http://username:password@ip:port"",
  "proxy3": "http://username:password@ip:port"
}
```

**Format:** `http://username:password@ip:port`

Then use a specific proxy when running the scraper:

```bash
python3 instagram_comment_scraper.py \
  --urls urls.txt \
  --proxy-map proxies.json \
  --proxy-name proxy1 \
  --expand-replies \
  --skip-complete
```

**Tips:**
- You can add multiple proxies to the JSON file and switch between them using `--proxy-name`
- Test each proxy individually to find the most reliable one
- If a proxy fails, try another one from your list

## Troubleshooting

### Instagram Challenge/Verification

If Instagram shows a verification challenge:
1. Run without `--headless` so you can see the browser
2. Complete the verification manually in the open browser
3. The script waits up to 5 minutes (configurable with `--manual-challenge-wait`)
4. After successful verification, session cookies are saved for future runs

### Login Issues

- Ensure credentials are correct and the account isn't locked
- Try running without `--headless` to see what's happening
- Check if Instagram requires two-factor authentication
- Delete `work/instagram_session_cookies.json` to force fresh login

### No Comments Found

- Increase `--max-scrolls` to load more comments
- Check if the post has comments enabled
- Verify the post URL is correct and accessible

## Important Notes

- **Use responsibly** - Only scrape content you're permitted to access
- **Rate limits** - Keep scraping rates modest to avoid being flagged
- **Terms of service** - Ensure your usage complies with Instagram's terms
- **Cookie security** - Session cookies are saved with restricted permissions where possible
