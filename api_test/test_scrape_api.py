"""Test script for POST /api/v1/scrape endpoint.

Usage:
    # Basic test (server must be running on localhost:8000)
    python api_test/test_scrape_api.py

    # Custom URL and options
    python api_test/test_scrape_api.py \
        --profile-link "https://www.instagram.com/halei.lawyer/reels/" \
        --max-reels 5 \
        --auth-file path/to/session.json \
        --base-url http://localhost:8000
"""

import argparse
import csv
import io
import sys
import time

import requests

EXPECTED_COLUMNS = ["link", "id", "views", "likes", "comments", "scraped_at"]


def test_scrape(base_url: str, profile_link: str, max_reels: int, auth_file: str | None):
    url = f"{base_url}/api/v1/scrape"

    data = {
        "profile_link": profile_link,
        "max_reels": str(max_reels),
    }

    files = {}
    if auth_file:
        files["auth_file"] = ("session.json", open(auth_file, "rb"), "application/json")

    print(f"POST {url}")
    print(f"  profile_link = {profile_link}")
    print(f"  max_reels    = {max_reels}")
    if auth_file:
        print(f"  auth_file    = {auth_file}")
    print()

    start = time.time()
    try:
        resp = requests.post(url, data=data, files=files if files else None, timeout=300)
    except requests.ConnectionError:
        print(f"FAIL: Cannot connect to {base_url}. Is the server running?")
        sys.exit(1)
    elapsed = time.time() - start

    print(f"Status: {resp.status_code} ({elapsed:.1f}s)")

    # --- Check error responses ---
    if resp.status_code != 200:
        print(f"FAIL: Expected 200, got {resp.status_code}")
        print(f"  Body: {resp.text[:500]}")
        return False

    # --- Validate Content-Type ---
    content_type = resp.headers.get("content-type", "")
    if "text/csv" not in content_type:
        print(f"FAIL: Expected text/csv content-type, got: {content_type}")
        return False
    print(f"Content-Type: {content_type}")

    # --- Validate Content-Disposition ---
    disposition = resp.headers.get("content-disposition", "")
    if "attachment" not in disposition or ".csv" not in disposition:
        print(f"WARN: Unexpected Content-Disposition: {disposition}")
    else:
        print(f"Content-Disposition: {disposition}")

    # --- Parse CSV ---
    text = resp.text
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        print("FAIL: CSV has no data rows")
        return False

    # --- Validate columns ---
    actual_columns = reader.fieldnames or []
    if actual_columns != EXPECTED_COLUMNS:
        print(f"FAIL: Column mismatch")
        print(f"  Expected: {EXPECTED_COLUMNS}")
        print(f"  Got:      {actual_columns}")
        return False
    print(f"Columns: {actual_columns}")

    # --- Validate row count ---
    print(f"Rows: {len(rows)}")
    if len(rows) > max_reels:
        print(f"FAIL: Got {len(rows)} rows but max_reels was {max_reels}")
        return False

    # --- Validate row content ---
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

    # --- Print sample rows ---
    print("\nSample rows:")
    for row in rows[:3]:
        print(f"  link={row['link'][:60]}...  id={row['id']}  "
              f"views={row['views']}  likes={row['likes']}  "
              f"comments={row['comments']}  scraped_at={row['scraped_at']}")
    if len(rows) > 3:
        print(f"  ... and {len(rows) - 3} more rows")

    # --- Save CSV locally ---
    out_path = "api_test/response.csv"
    with open(out_path, "w") as f:
        f.write(text)
    print(f"\nFull CSV saved to {out_path}")

    print("\nPASS: All checks passed")
    return True


def test_bad_request(base_url: str):
    """Test that invalid inputs return proper error responses."""
    url = f"{base_url}/api/v1/scrape"
    passed = True

    # Missing required field
    print("\n--- Test: missing profile_link ---")
    resp = requests.post(url, data={"max_reels": "5"})
    if resp.status_code == 422:
        print(f"  OK: Got 422 as expected")
    else:
        print(f"  FAIL: Expected 422, got {resp.status_code}")
        passed = False

    # Invalid username
    print("\n--- Test: invalid profile_link ---")
    resp = requests.post(url, data={"profile_link": "!!!invalid!!!", "max_reels": "5"})
    if resp.status_code == 400:
        print(f"  OK: Got 400 as expected")
    else:
        print(f"  FAIL: Expected 400, got {resp.status_code}")
        passed = False

    # max_reels out of range
    print("\n--- Test: max_reels=0 ---")
    resp = requests.post(url, data={"profile_link": "testuser", "max_reels": "0"})
    if resp.status_code == 422:
        print(f"  OK: Got 422 as expected")
    else:
        print(f"  FAIL: Expected 422, got {resp.status_code}")
        passed = False

    return passed


def main():
    parser = argparse.ArgumentParser(description="Test the /api/v1/scrape endpoint")
    parser.add_argument(
        "--profile-link",
        default="https://www.instagram.com/halei.lawyer/reels/",
        help="Profile URL to scrape",
    )
    parser.add_argument("--max-reels", type=int, default=5, help="Max reels (default: 5)")
    parser.add_argument("--auth-file", default=None, help="Path to session JSON file")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip the live scrape test")
    args = parser.parse_args()

    print("=" * 60)
    print("API Test: POST /api/v1/scrape")
    print("=" * 60)

    all_passed = True

    # Validation tests (fast, no scraping)
    print("\n>>> Validation tests")
    if not test_bad_request(args.base_url):
        all_passed = False

    # Live scrape test
    if not args.skip_scrape:
        print("\n>>> Live scrape test")
        if not test_scrape(args.base_url, args.profile_link, args.max_reels, args.auth_file):
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
