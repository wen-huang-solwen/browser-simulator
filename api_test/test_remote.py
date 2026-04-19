"""Test script calling the remote scrape API at http://172.104.86.234:8000/

Usage:
    python api_test/test_remote.py

    # Custom profile
    python api_test/test_remote.py --profile-link "https://www.instagram.com/someuser/reels/" --max-reels 10

    # With auth file
    python api_test/test_remote.py --auth-file .auth/session.json
"""

import argparse
import csv
import io
import sys
import time

import requests

BASE_URL = "http://172.104.86.234:8000"
EXPECTED_COLUMNS = ["link", "id", "views", "likes", "comments", "scraped_at"]


def check_server():
    """Verify the remote server is reachable."""
    print(f"Checking server at {BASE_URL} ...")
    try:
        resp = requests.get(BASE_URL, timeout=10)
        print(f"  Server is up (HTTP {resp.status_code})")
        return True
    except requests.ConnectionError:
        print(f"  FAIL: Cannot connect to {BASE_URL}")
        return False
    except requests.Timeout:
        print(f"  FAIL: Connection timed out")
        return False


def check_session_status():
    """Check which platform sessions are available on the remote server."""
    print(f"\nChecking session status ...")
    try:
        resp = requests.get(f"{BASE_URL}/api/session/status", timeout=10)
        if resp.status_code == 200:
            status = resp.json()
            for platform, available in status.items():
                label = "available" if available else "not configured"
                print(f"  {platform}: {label}")
            return status
        else:
            print(f"  WARN: Got HTTP {resp.status_code}")
            return {}
    except Exception as e:
        print(f"  WARN: {e}")
        return {}


def test_validation():
    """Test that invalid inputs return proper error responses."""
    url = f"{BASE_URL}/api/v1/scrape"
    passed = True

    print("\n--- Test: missing profile_link ---")
    resp = requests.post(url, data={"max_reels": "5"}, timeout=30)
    if resp.status_code == 422:
        print(f"  OK: Got 422 as expected")
    else:
        print(f"  FAIL: Expected 422, got {resp.status_code}")
        passed = False

    print("\n--- Test: invalid profile_link ---")
    resp = requests.post(url, data={"profile_link": "!!!invalid!!!", "max_reels": "5"}, timeout=30)
    if resp.status_code == 400:
        print(f"  OK: Got 400 as expected")
    else:
        print(f"  FAIL: Expected 400, got {resp.status_code}")
        passed = False

    print("\n--- Test: max_reels=0 (out of range) ---")
    resp = requests.post(url, data={"profile_link": "testuser", "max_reels": "0"}, timeout=30)
    if resp.status_code == 422:
        print(f"  OK: Got 422 as expected")
    else:
        print(f"  FAIL: Expected 422, got {resp.status_code}")
        passed = False

    return passed


def test_scrape(profile_link: str, max_reels: int, auth_file: str | None):
    """Run a live scrape against the remote server and validate the CSV response."""
    url = f"{BASE_URL}/api/v1/scrape"

    data = {
        "profile_link": profile_link,
        "max_reels": str(max_reels),
    }

    files = {}
    if auth_file:
        files["auth_file"] = ("session.json", open(auth_file, "rb"), "application/json")

    print(f"\nPOST {url}")
    print(f"  profile_link = {profile_link}")
    print(f"  max_reels    = {max_reels}")
    if auth_file:
        print(f"  auth_file    = {auth_file}")
    print()

    # Large max_reels requires more time — the scraper scrolls with human-like
    # delays (2-4s per scroll) and waits for 8 consecutive empty scrolls before
    # stopping, so scraping can take several minutes for large requests.
    timeout = max(300, max_reels * 2)
    print(f"  timeout      = {timeout}s")

    start = time.time()
    try:
        resp = requests.post(url, data=data, files=files if files else None, timeout=timeout)
    except requests.ConnectionError:
        print(f"FAIL: Lost connection to {BASE_URL}")
        return False
    except requests.Timeout:
        print(f"FAIL: Request timed out ({timeout}s)")
        return False
    elapsed = time.time() - start

    print(f"Status: {resp.status_code} ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"FAIL: Expected 200, got {resp.status_code}")
        print(f"  Body: {resp.text[:500]}")
        return False

    # Validate Content-Type
    content_type = resp.headers.get("content-type", "")
    if "text/csv" not in content_type:
        print(f"FAIL: Expected text/csv, got: {content_type}")
        return False
    print(f"Content-Type: {content_type}")

    # Validate Content-Disposition
    disposition = resp.headers.get("content-disposition", "")
    if "attachment" in disposition and ".csv" in disposition:
        print(f"Content-Disposition: {disposition}")
    else:
        print(f"WARN: Unexpected Content-Disposition: {disposition}")

    # Parse CSV
    text = resp.text
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        print("FAIL: CSV has no data rows")
        return False

    # Validate columns
    actual_columns = reader.fieldnames or []
    if actual_columns != EXPECTED_COLUMNS:
        print(f"FAIL: Column mismatch")
        print(f"  Expected: {EXPECTED_COLUMNS}")
        print(f"  Got:      {actual_columns}")
        return False
    print(f"Columns: {actual_columns}")

    # Validate row count
    print(f"Rows: {len(rows)}")
    if len(rows) > max_reels:
        print(f"FAIL: Got {len(rows)} rows but max_reels was {max_reels}")
        return False
    if len(rows) < max_reels:
        print(f"  Note: Got {len(rows)} rows (less than max_reels={max_reels})."
              f" The profile likely has only {len(rows)} reels available.")

    # Validate row content
    errors = []
    for i, row in enumerate(rows):
        if not row["link"]:
            errors.append(f"  Row {i+1}: missing 'link'")
        if not row["id"]:
            errors.append(f"  Row {i+1}: missing 'id'")
        if not row["scraped_at"]:
            errors.append(f"  Row {i+1}: missing 'scraped_at'")
        if row["views"] and not row["views"].lstrip("-").isdigit():
            errors.append(f"  Row {i+1}: 'views' is not numeric: {row['views']}")
        if row["likes"] and not row["likes"].lstrip("-").isdigit():
            errors.append(f"  Row {i+1}: 'likes' is not numeric: {row['likes']}")
        if row["comments"] and not row["comments"].lstrip("-").isdigit():
            errors.append(f"  Row {i+1}: 'comments' is not numeric: {row['comments']}")

    if errors:
        print("FAIL: Row validation errors:")
        for e in errors:
            print(e)
        return False

    # Print sample rows
    print("\nSample rows:")
    for row in rows[:5]:
        print(f"  link={row['link'][:60]}...  id={row['id']}  "
              f"views={row['views']}  likes={row['likes']}  "
              f"comments={row['comments']}  scraped_at={row['scraped_at']}")
    if len(rows) > 5:
        print(f"  ... and {len(rows) - 5} more rows")

    # Save CSV locally
    out_path = "api_test/remote_response.csv"
    with open(out_path, "w") as f:
        f.write(text)
    print(f"\nFull CSV saved to {out_path}")

    print("\nPASS: All checks passed")
    return True


def test_api_docs():
    """Verify the API docs page is accessible."""
    print("\n--- Test: API docs page ---")
    try:
        resp = requests.get(f"{BASE_URL}/api/docs", timeout=10)
        if resp.status_code == 200 and "Reels Scraper API" in resp.text:
            print(f"  OK: API docs page is accessible")
            return True
        else:
            print(f"  FAIL: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test remote scrape API at 172.104.86.234:8000")
    parser.add_argument(
        "--profile-link",
        default="https://www.instagram.com/halei.lawyer/reels/",
        help="Profile URL to scrape",
    )
    parser.add_argument("--max-reels", type=int, default=5, help="Max reels (default: 5)")
    parser.add_argument("--auth-file", default=None, help="Path to local session JSON to upload")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip the live scrape test")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Remote API Test: {BASE_URL}")
    print("=" * 60)

    all_passed = True

    # 1. Server health check
    if not check_server():
        print("\nServer is not reachable. Aborting.")
        sys.exit(1)

    # 2. Session status
    check_session_status()

    # 3. API docs page
    if not test_api_docs():
        all_passed = False

    # 4. Validation tests
    print("\n>>> Validation tests")
    if not test_validation():
        all_passed = False

    # 5. Live scrape test
    if not args.skip_scrape:
        print("\n>>> Live scrape test")
        if not test_scrape(args.profile_link, args.max_reels, args.auth_file):
            all_passed = False
    else:
        print("\n>>> Skipping live scrape test (--skip-scrape)")

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
