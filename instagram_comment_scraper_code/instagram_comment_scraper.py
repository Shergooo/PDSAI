#!/usr/bin/env python3
"""
Production Instagram post comment scraper.

Credentials are never hard-coded. Provide them with:
  INSTAGRAM_USERNAME=... INSTAGRAM_PASSWORD=... python instagram_comment_scraper.py --urls urls.txt

The scraper persists each post as JSON and upserts all posts/comments into SQLite
so interrupted runs can resume without losing already collected data.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGGER = logging.getLogger("instagram-comment-scraper")

INSTAGRAM_HOME = "https://www.instagram.com/"

# Targets the span containing the main comment text body

# Updated CSS selectors based on working implementation
COMMENT_CSS_SELECTOR = (
    "div.html-div.xdj266r.x14z9mp.xat24cr.x1lziwak.xexx8yu.xyri2b.x18d9i69.x1c1uobl.x9f619.xjbqb8w.x78zum5.x15mokao.x1ga7v0g.x16uus16.xbiv7yw.x1uhb9sk.x1plvlek.xryxfnj.x1c4vz4f.x2lah0s.xdt5ytf.xqjyukv.x1qjc9v5.x1oa3qoh.x1nhvcw1"
)
COMMENT_TEXT_CSS_SELECTOR = (
    "span.x1lliihq.x1plvlek.xryxfnj.x1n2onr6.xyejjpt.x15dsfln.x193iq5w.xeuugli.x1fj9vlw.x13faqbe.x1vvkbs.x1s928wv.xhkezso.x1gmr53x.x1cpjm7i.x1fgarty.x1943h6x.x1i0vuye.xvs91rp.xo1l8bm.x5n08af"
)
USERNAME_CSS_SELECTOR = "span[class*='_ap3a']"
HEADER_CSS_SELECTOR = (
    "div.html-div.xdj266r.x14z9mp.xat24cr.x1lziwak.xexx8yu.xyri2b.x18d9i69.x1c1uobl.x9f619.xjbqb8w.x78zum5.x15mokao.x1ga7v0g.x16uus16.xbiv7yw.x1uhb9sk.x1plvlek.xryxfnj.x1c4vz4f.x2lah0s.x1q0g3np.xqjyukv.x6s0dn4.x1oa3qoh.x1nhvcw1"
)
SCROLLABLE_DIV_CSS_SELECTORS = [
    "div.x5yr21d.xw2csxc.x1odjw0f.x1n2onr6",
    "div[role='dialog'] div.x5yr21d.xw2csxc",
    "div[role='dialog'] div[style*='overflow']",
]
POST_TIME_CSS_SELECTOR = "time.x1p4m5qa"
LIKE_BUTTON_CSS_SELECTOR = "span.x1ypdohk.x1s688f.x2fvf9.xe9ewy2[role='button']"
VERIFIED_SVG_SELECTOR = "svg[aria-label='Verified']"
GIF_IMAGE_CSS_SELECTOR = "img.x12ol6y4"

COMMENT_NODE_XPATH = (
    ".//span[contains(@class, '_aade') or contains(@class, '_ap3a')]"
)
COMMENTS_CONTAINER_XPATHS = (
    "//div[@role='dialog']//ul",
    "//article//ul",
    "//main//ul",
)
POST_TIME_XPATH = (
    "//article//time[1] | "
    "//div[@role='dialog']//time[not(ancestor::ul)][1] | "
    "//main//time[1]"
)
VERIFIED_XPATH = (
    "//article//header//*[@aria-label='Verified'] | "
    "//div[@role='dialog']//header//*[@aria-label='Verified']"
)


@dataclass(frozen=True)
class CommentRecord:
    username: str
    comment: str
    likes: int
    replies: int
    is_verified: bool


@dataclass(frozen=True)
class PostRecord:
    url: str
    shortcode: str
    post_timestamp: str | None
    post_publisher_is_verified: bool
    post_total_likes: int
    post_total_comments_count: int
    comments: list[CommentRecord]


def parse_count(text: str | None) -> int:
    """Parse count like '6.6K' or '2,029' to integer."""
    if not text:
        return 0
    text = text.strip().replace(',', '')
    
    # Check for K (thousands)
    if 'K' in text.upper():
        number = float(text.upper().replace('K', ''))
        return int(number * 1000)
    
    # Check for M (millions)
    if 'M' in text.upper():
        number = float(text.upper().replace('M', ''))
        return int(number * 1000000)
    
    # Just a number
    try:
        return int(float(text))
    except:
        return 0


def shortcode_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    for marker in ("p", "reel", "reels", "tv"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                return parts[index + 1]
    return parts[-1] if parts else hashlib.sha256(url.encode()).hexdigest()[:12]


def normalize_post_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"https://www.instagram.com/{url.lstrip('/')}"
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl().rstrip("/") + "/"


def read_urls(path: Path) -> list[str]:
    urls = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(normalize_post_url(line))
    seen = set()
    return [url for url in urls if not (url in seen or seen.add(url))]


def jitter(min_seconds: float = 0.4, max_seconds: float = 1.2) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def slow_type(element, text: str, min_delay: float = 0.08, max_delay: float = 0.22) -> None:
    for char in text:
        element.send_keys(char)
        jitter(min_delay, max_delay)


def load_proxy_from_map(proxy_map_path: Path | None, proxy_name: str | None) -> str | None:
    if not proxy_map_path and not proxy_name:
        return None
    if not proxy_map_path or not proxy_name:
        raise ValueError("--proxy-map and --proxy-name must be used together")

    proxy_map = json.loads(proxy_map_path.read_text(encoding="utf-8"))
    proxy = proxy_map.get(proxy_name)
    if not proxy:
        available = ", ".join(sorted(proxy_map))
        raise ValueError(f"Proxy name {proxy_name!r} was not found. Available names: {available}")
    return proxy


class ScrapeStore:
    def __init__(self, db_path: Path, json_dir: Path) -> None:
        self.db_path = db_path
        self.json_dir = json_dir
        self.json_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                shortcode TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                post_timestamp TEXT,
                post_publisher_is_verified INTEGER NOT NULL DEFAULT 0,
                post_total_likes INTEGER NOT NULL DEFAULT 0,
                post_total_comments_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shortcode TEXT NOT NULL REFERENCES posts(shortcode) ON DELETE CASCADE,
                username TEXT NOT NULL,
                comment TEXT NOT NULL,
                likes INTEGER NOT NULL DEFAULT 0,
                replies INTEGER NOT NULL DEFAULT 0,
                is_verified INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.conn.commit()

    def is_complete(self, shortcode: str) -> bool:
        row = self.conn.execute(
            "SELECT status, post_total_comments_count FROM posts WHERE shortcode = ?",
            (shortcode,),
        ).fetchone()
        return bool(row and row[0] == "ok" and row[1] > 0)

    def save_post(self, post: PostRecord) -> Path:
        payload = {
            "url": post.url,
            "post_timestamp": post.post_timestamp,
            "post_publisher_is_verified": post.post_publisher_is_verified,
            "post_total_likes": post.post_total_likes,
            "post_total_comments_count": post.post_total_comments_count,
            "comments": [asdict(comment) for comment in post.comments],
        }

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO posts (
                    shortcode, url, post_timestamp, post_publisher_is_verified,
                    post_total_likes, post_total_comments_count, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL)
                ON CONFLICT(shortcode) DO UPDATE SET
                    url = excluded.url,
                    post_timestamp = excluded.post_timestamp,
                    post_publisher_is_verified = excluded.post_publisher_is_verified,
                    post_total_likes = excluded.post_total_likes,
                    post_total_comments_count = excluded.post_total_comments_count,
                    status = 'ok',
                    error = NULL
                """,
                (
                    post.shortcode,
                    post.url,
                    post.post_timestamp,
                    int(post.post_publisher_is_verified),
                    post.post_total_likes,
                    post.post_total_comments_count,
                ),
            )
            self.conn.execute("DELETE FROM comments WHERE shortcode = ?", (post.shortcode,))
            self.conn.executemany(
                """
                INSERT INTO comments (
                    shortcode, username, comment, likes, replies, is_verified
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        post.shortcode,
                        comment.username,
                        comment.comment,
                        comment.likes,
                        comment.replies,
                        int(comment.is_verified),
                    )
                    for comment in post.comments
                ],
            )

        output_path = self.json_dir / f"{post.shortcode}.json"
        atomic_json_dump(output_path, payload)
        return output_path

    def save_error(self, shortcode: str, url: str, error: Exception) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO posts (shortcode, url, status, error)
                VALUES (?, ?, 'error', ?)
                ON CONFLICT(shortcode) DO UPDATE SET
                    url = excluded.url,
                    status = 'error',
                    error = excluded.error
                """,
                (
                    shortcode,
                    url,
                    f"{type(error).__name__}: {error}",
                ),
            )


def atomic_json_dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temp_file:
        json.dump(payload, temp_file, ensure_ascii=False, indent=2)
        temp_file.write("\n")
        temp_name = temp_file.name
    os.replace(temp_name, path)


class InstagramCommentScraper:
    def __init__(
        self,
        driver: WebDriver,
        wait_seconds: int,
        cookie_path: Path,
        expand_replies: bool,
        scroll_stagnation_limit: int,
        max_scrolls: int,
        login_pause: tuple[float, float],
        manual_challenge_wait: int,
        post_delay: tuple[float, float],
    ) -> None:
        self.driver = driver
        self.wait = WebDriverWait(driver, wait_seconds)
        self.cookie_path = cookie_path
        self.expand_replies = expand_replies
        self.scroll_stagnation_limit = scroll_stagnation_limit
        self.max_scrolls = max_scrolls
        self.login_pause = login_pause
        self.manual_challenge_wait = manual_challenge_wait
        self.post_delay = post_delay

    def login(self, username: str, password: str) -> None:
        self.driver.get(INSTAGRAM_HOME)
        if self._load_cookies():
            self.driver.refresh()
            jitter(*self.login_pause)
            if self._is_authenticated(navigate=True):
                LOGGER.info("Reused existing authenticated session")
                return

        self.driver.get(INSTAGRAM_HOME)
        jitter(*self.login_pause)
        self._click_optional("Decline optional cookies")
        jitter(*self.login_pause)
        self._fill_login_form(username, password)
        self._wait_for_manual_checkpoint()
        self._click_post_login_dialogs()

        if not self._is_authenticated(navigate=True):
            raise RuntimeError("Login did not complete. Check credentials or complete any Instagram challenge manually.")

        self._save_cookies()
        LOGGER.info("Logged in and saved browser session cookies")

    def scrape_post(self, url: str) -> PostRecord:
        self.driver.get(url)
        self._click_optional("Decline optional cookies")
        self._wait_for_post()
        if self.expand_replies:
            self._expand_visible_reply_threads()

        comments = self._collect_all_comments()
        shortcode = shortcode_from_url(url)
        
        # Skip the first comment if it's the caption (as per the provided code)
        real_comments = comments[1:] if len(comments) > 1 else []
        
        return PostRecord(
            url=url,
            shortcode=shortcode,
            post_timestamp=self._post_timestamp(),
            post_publisher_is_verified=self._is_post_publisher_verified(),
            post_total_likes=self._post_like_count(),
            post_total_comments_count=self._post_comments_count(),
            comments=real_comments,
        )

    def _is_post_publisher_verified(self) -> bool:
        """Check if the post publisher is verified using CSS selector."""
        try:
            header_div = self.driver.find_element(By.CSS_SELECTOR, HEADER_CSS_SELECTOR)
            return self._is_user_verified(header_div)
        except:
            # Fallback to XPath
            return bool(self.driver.find_elements(By.XPATH, VERIFIED_XPATH))

    def _wait_for_post(self) -> None:
        self.wait.until(
            EC.presence_of_element_located((By.XPATH, "//article | //main | //div[@role='dialog']"))
        )
        jitter(1, 2)

    def _fill_login_form(self, username: str, password: str) -> None:
        username_input = self.wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "input[name='email'], input[name='username'], "
                    "input[autocomplete*='username'], "
                    "input[aria-label*='username' i], input[aria-label*='email' i]",
                )
            )
        )
        username_input.clear()
        jitter(0.8, 1.8)
        slow_type(username_input, username)
        jitter(*self.login_pause)

        password_input = self.wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[name='pass'], input[name='password'], input[type='password']")
            )
        )
        password_input.clear()
        jitter(0.8, 1.8)
        slow_type(password_input, password)
        jitter(*self.login_pause)
        password_input.send_keys(Keys.ENTER)
        print("Waiting 3 minutes after login in...")
        time.sleep(180)
        jitter(max(self.login_pause[0], 6), max(self.login_pause[1], 12))

    def _click_post_login_dialogs(self) -> None:
        for label in ("Not now", "Not Now", "Decline optional cookies"):
            self._click_optional(label, timeout=5)
            jitter(*self.login_pause)

    def _click_optional(self, text: str, timeout: int = 4) -> bool:
        xpath = (
            f"//button[normalize-space()={json.dumps(text)}] | "
            f"//*[@role='button' and normalize-space()={json.dumps(text)}] | "
            f"//*[normalize-space()={json.dumps(text)}]"
        )
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            element.click()
            jitter(0.8, 1.8)
            return True
        except TimeoutException:
            return False

    def _is_authenticated(self, navigate: bool = False) -> bool:
        if navigate:
            self.driver.get(INSTAGRAM_HOME)
            jitter(*self.login_pause)
        current_url = self.driver.current_url.lower()
        if "/accounts/login" in current_url or "/challenge" in current_url or self._page_has_checkpoint():
            return False
        login_fields = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
        return not login_fields

    def _page_has_checkpoint(self) -> bool:
        checkpoint_markers = (
            "verify",
            "not a robot",
            "suspicious",
            "challenge",
            "security code",
            "confirm your",
            "help us confirm",
        )
        try:
            page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            return False
        return any(marker in page_text for marker in checkpoint_markers)

    def _wait_for_manual_checkpoint(self) -> None:
        if self.manual_challenge_wait <= 0:
            return

        if not self._page_has_checkpoint():
            return

        LOGGER.warning(
            "Instagram displayed a verification/checkpoint page. "
            "Complete it in the open browser; waiting up to %s seconds.",
            self.manual_challenge_wait,
        )
        deadline = time.time() + self.manual_challenge_wait
        while time.time() < deadline:
            if self._is_authenticated(navigate=False):
                LOGGER.info("Manual verification appears complete")
                return
            jitter(5, 8)

    def _load_cookies(self) -> bool:
        if not self.cookie_path.exists():
            return False
        self.driver.get(INSTAGRAM_HOME)
        cookies = json.loads(self.cookie_path.read_text(encoding="utf-8"))
        for cookie in cookies:
            cookie.pop("sameSite", None)
            try:
                self.driver.add_cookie(cookie)
            except Exception:
                LOGGER.debug("Skipped incompatible cookie: %s", cookie.get("name"))
        return True

    def _save_cookies(self) -> None:
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_dump(self.cookie_path, self.driver.get_cookies())
        try:
            os.chmod(self.cookie_path, 0o600)
        except OSError:
            LOGGER.warning("Could not restrict cookie file permissions")

    def _post_timestamp(self) -> str | None:
        """Extract post timestamp using CSS selector."""
        try:
            time_el = self.driver.find_element(By.CSS_SELECTOR, POST_TIME_CSS_SELECTOR)
            return time_el.get_attribute("datetime") or time_el.text.strip()
        except:
            # Fallback to XPath
            for element in self.driver.find_elements(By.XPATH, POST_TIME_XPATH):
                value = element.get_attribute("datetime")
                if value:
                    return value
        return None

    def _post_like_count(self) -> int:
        """Extract total likes using CSS selector."""
        try:
            like_el = self.driver.find_element(By.CSS_SELECTOR, LIKE_BUTTON_CSS_SELECTOR)
            like_text = like_el.text.strip()
            return parse_count(like_text)
        except:
            # Fallback to XPath
            candidates = self.driver.find_elements(
                By.XPATH,
                "//*[contains(translate(., 'LIKES', 'likes'), 'likes') or "
                "contains(translate(., 'VIEWS', 'views'), 'views')]",
            )
            for element in candidates:
                count = parse_count(element.text)
                if count:
                    return count
        return 0

    def _post_comments_count(self) -> int:
        """Extract total comments count from post metadata."""
        try:
            # Try to get comments from the like/comment buttons
            comment_els = self.driver.find_elements(By.CSS_SELECTOR, LIKE_BUTTON_CSS_SELECTOR)
            
            if len(comment_els) >= 2:
                comment_text = comment_els[1].text.strip()
                return parse_count(comment_text)
            else:
                # Alternative: look for "comments" text
                comment_el = self.driver.find_element(By.XPATH, 
                    "//span[contains(text(), 'comments') or contains(text(), 'comment')]")
                comment_text = comment_el.text.strip()
                numbers = re.findall(r'[\d,.]+', comment_text)
                if numbers:
                    return parse_count(numbers[0])
        except:
            pass
        return 0

    def _comments_container(self):
        for xpath in COMMENTS_CONTAINER_XPATHS:
            elements = self.driver.find_elements(By.XPATH, xpath)
            if elements:
                return elements[-1]
        return None

    def _scroll_comments_modal(self) -> None:
        """Scroll the comments modal to load more comments (limited to max_scrolls)."""
        LOGGER.info("🔄 Loading comments from modal...")
        
        # Find the scrollable comments container
        scrollable_div = None
        
        for selector in SCROLLABLE_DIV_CSS_SELECTORS:
            try:
                scrollable_div = self.driver.find_element(By.CSS_SELECTOR, selector)
                if scrollable_div:
                    break
            except:
                pass
        
        previous_count = 0
        
        for scroll_num in range(self.max_scrolls):
            # Get current comment count using the same CSS selector as extraction
            comment_divs = self.driver.find_elements(By.CSS_SELECTOR, COMMENT_CSS_SELECTOR)
            current_count = len(comment_divs)
            LOGGER.info(f"  Scroll {scroll_num + 1}/{self.max_scrolls}: Found {current_count} comments")
            
            # Check if we have new comments
            if current_count == previous_count:
                LOGGER.info(f"  No new comments loaded. Stopping early.")
                break
                
            previous_count = current_count
            
            # Scroll the comments container
            try:
                if scrollable_div:
                    self.driver.execute_script(
                        "arguments[0].scrollTop = arguments[0].scrollHeight;",
                        scrollable_div
                    )
                else:
                    self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except:
                pass
            
            jitter(1.3, 2.4)
            
            # Try to click "Load more comments" button if it appears
            try:
                load_btn = self.driver.find_element(By.XPATH, "//span[contains(text(), 'Load more comments')]")
                self.driver.execute_script("arguments[0].click();", load_btn)
                LOGGER.info("  Clicked 'Load more comments'")
                jitter(1.3, 2.4)
            except:
                pass
        
        LOGGER.info(f"✅ Finished loading after {scroll_num + 1} scrolls. Total comments: {current_count}")

    def _is_user_verified(self, element) -> bool:
        """Check if a user is verified by looking for verified badge in the element."""
        try:
            verified_svg = element.find_element(By.CSS_SELECTOR, VERIFIED_SVG_SELECTOR)
            if verified_svg:
                return True
        except:
            pass
        return False

    def _extract_comments_with_engagement(self) -> list[CommentRecord]:
        """Extract comments with likes and replies count using CSS selectors."""
        comment_divs = self.driver.find_elements(By.CSS_SELECTOR, COMMENT_CSS_SELECTOR)
        comments = []
        seen = set()
        
        for div in comment_divs:
            try:
                # Get username
                username_el = div.find_element(By.CSS_SELECTOR, USERNAME_CSS_SELECTOR)
                username = username_el.text.strip()
                
                # Check if commenter is verified
                is_verified = self._is_user_verified(div)
                
                # Get full text for pattern matching
                full_text = div.text
                
                # Extract likes (with commas)
                likes = 0
                like_patterns = [
                    r'([\d,]+)\s+likes',
                    r'([\d,]+)\s+like',
                    r'([\d,]+)\s+❤️',
                ]
                for pattern in like_patterns:
                    match = re.search(pattern, full_text, re.IGNORECASE)
                    if match:
                        likes = parse_count(match.group(1))
                        break
                
                # Extract replies (with commas) - only use specific "View all" patterns to avoid matching usernames
                replies = 0
                reply_patterns = [
                    r'View all ([\d,]+)\s+replies',
                    r'View all ([\d,]+)\s+reply',
                ]
                for pattern in reply_patterns:
                    match = re.search(pattern, full_text, re.IGNORECASE)
                    if match:
                        replies = parse_count(match.group(1))
                        break
                
                # Check for GIF comment
                gif_els = div.find_elements(By.CSS_SELECTOR, GIF_IMAGE_CSS_SELECTOR)
                found_gif = False
                
                for gif in gif_els:
                    parent = gif.find_element(By.XPATH, "..")
                    parent_class = parent.get_attribute('class') or ''
                    
                    if 'xbmvrgn' not in parent_class:
                        comment_text = f"[GIF: {gif.get_attribute('src')}]"
                        key = (username, comment_text)
                        if key not in seen:
                            seen.add(key)
                            comments.append(CommentRecord(
                                username=username,
                                comment=comment_text,
                                likes=likes,
                                replies=replies,
                                is_verified=is_verified
                            ))
                        found_gif = True
                        break
                
                # If no GIF found, look for text comment
                if not found_gif:
                    comment_spans = div.find_elements(By.CSS_SELECTOR, COMMENT_TEXT_CSS_SELECTOR)
                    
                    for span in comment_spans:
                        text = span.text.strip()
                        
                        if not text or text == username:
                            continue
                        
                        # Skip timestamps and UI elements
                        skip_patterns = [
                            r'\d+\s+[whm]\s+ago',
                            r'like',
                            r'reply',
                            r'View all.*replies',
                        ]
                        
                        should_skip = False
                        for pattern in skip_patterns:
                            if re.search(pattern, text, re.IGNORECASE):
                                should_skip = True
                                break
                        
                        if should_skip:
                            continue
                        
                        key = (username, text)
                        if key not in seen:
                            seen.add(key)
                            comments.append(CommentRecord(
                                username=username,
                                comment=text,
                                likes=likes,
                                replies=replies,
                                is_verified=is_verified
                            ))
                        break
                                
            except Exception as e:
                LOGGER.debug(f"Skipped one comment block: {e}")
                continue
        
        return comments


    def _collect_all_comments(self) -> list[CommentRecord]:
        """Collect all comments using the new scroll and extraction logic."""
        # First, scroll to load all comments
        self._scroll_comments_modal()
        
        # Then extract comments using the new CSS selector approach
        return self._extract_comments_with_engagement()

    def _expand_visible_reply_threads(self) -> None:
        reply_xpath = (
            "//*[contains(., 'View all') and contains(., 'replies')] | "
            "//*[contains(., 'View replies')]"
        )
        for element in self.driver.find_elements(By.XPATH, reply_xpath)[:25]:
            try:
                self.driver.execute_script("arguments[0].click();", element)
                jitter(0.2, 0.6)
            except Exception:
                continue



def build_driver(args: argparse.Namespace) -> WebDriver:
    options = ChromeOptions()
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1440,1100")
    options.add_argument("--lang=en-US")

    proxy = args.proxy or load_proxy_from_map(args.proxy_map, args.proxy_name)
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")

    if args.user_data_dir:
        options.add_argument(f"--user-data-dir={args.user_data_dir}")

    return webdriver.Chrome(options=options)


def credentials_from_environment_or_prompt() -> tuple[str, str]:
    username = os.getenv("INSTAGRAM_USERNAME") or input("Instagram username: ").strip()
    password = os.getenv("INSTAGRAM_PASSWORD") or getpass.getpass("Instagram password: ")
    if not username or not password:
        raise ValueError("Both Instagram username and password are required")
    return username, password


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Instagram post comments from a URL list.")
    parser.add_argument("--urls", required=True, type=Path, help="Text file with one Instagram post URL per line.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/instagram_posts"))
    parser.add_argument("--db", type=Path, default=Path("outputs/instagram_scrape.sqlite3"))
    parser.add_argument("--cookie-file", type=Path, default=Path("work/instagram_session_cookies.json"))
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode.")
    parser.add_argument("--proxy", help="Optional proxy server, for example http://user:pass@host:port.")
    parser.add_argument("--proxy-map", type=Path, help="JSON file mapping proxy names to proxy URLs.")
    parser.add_argument("--proxy-name", help="Name inside --proxy-map to use for this run.")
    parser.add_argument("--user-data-dir", type=Path, help="Optional dedicated Chrome profile directory.")
    parser.add_argument("--wait", type=int, default=20, help="Selenium wait timeout in seconds.")
    parser.add_argument("--max-scrolls", type=int, default=250, help="Maximum comment scroll attempts per post.")
    parser.add_argument("--stagnation-limit", type=int, default=8, help="Stop after this many unchanged scroll rounds.")
    parser.add_argument("--login-min-delay", type=float, default=4.0, help="Minimum pause around login actions.")
    parser.add_argument("--login-max-delay", type=float, default=9.0, help="Maximum pause around login actions.")
    parser.add_argument("--post-min-delay", type=float, default=8.0, help="Minimum pause between post URLs.")
    parser.add_argument("--post-max-delay", type=float, default=18.0, help="Maximum pause between post URLs.")
    parser.add_argument(
        "--manual-challenge-wait",
        type=int,
        default=300,
        help="Seconds to wait while you manually complete a visible Instagram verification.",
    )
    parser.add_argument("--expand-replies", action="store_true", help="Click visible reply expanders while scraping.")
    parser.add_argument("--skip-complete", action="store_true", help="Skip posts already completed in SQLite.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.login_min_delay > args.login_max_delay:
        parser.error("--login-min-delay cannot be greater than --login-max-delay")
    if args.post_min_delay > args.post_max_delay:
        parser.error("--post-min-delay cannot be greater than --post-max-delay")
    return args


def scrape_urls(urls: Iterable[str], args: argparse.Namespace) -> None:
    username, password = credentials_from_environment_or_prompt()
    store = ScrapeStore(args.db, args.output_dir)
    driver = build_driver(args)
    scraper = InstagramCommentScraper(
        driver=driver,
        wait_seconds=args.wait,
        cookie_path=args.cookie_file,
        expand_replies=args.expand_replies,
        scroll_stagnation_limit=args.stagnation_limit,
        max_scrolls=args.max_scrolls,
        login_pause=(args.login_min_delay, args.login_max_delay),
        manual_challenge_wait=args.manual_challenge_wait,
        post_delay=(args.post_min_delay, args.post_max_delay),
    )

    try:
        scraper.login(username, password)
        for index, url in enumerate(urls, start=1):
            shortcode = shortcode_from_url(url)
            if args.skip_complete and store.is_complete(shortcode):
                LOGGER.info("[%s] Skipping already completed post: %s", index, url)
                continue
            LOGGER.info("[%s] Opening %s", index, url)
            try:
                post = scraper.scrape_post(url)
                if post.post_total_comments_count == 0:
                    LOGGER.info("[%s] Skipping post with 0 comments: %s", index, url)
                    continue
                output_path = store.save_post(post)
                LOGGER.info(
                    "[%s] Saved %s comments for %s to %s",
                    index,
                    post.post_total_comments_count,
                    shortcode,
                    output_path,
                )
            except Exception as exc:
                store.save_error(shortcode, url, exc)
                LOGGER.exception("[%s] Failed to scrape %s", index, url)
            jitter(*scraper.post_delay)
    finally:
        store.close()
        driver.quit()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    urls = read_urls(args.urls)
    if not urls:
        raise SystemExit("No URLs found in input file.")
    scrape_urls(urls, args)


if __name__ == "__main__":
    main()
