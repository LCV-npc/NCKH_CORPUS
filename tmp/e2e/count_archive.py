import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup


def main():
    base = "https://tapchiyhcd.vn/index.php/yhcd/issue/archive"
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    years = {str(year): {} for year in range(2020, 2027)}
    pages = 0
    for page in range(1, 41):
        url = base if page == 1 else f"{base}/{page}"
        response = session.get(url, timeout=60, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        links = [a for a in soup.find_all("a", href=True) if "/issue/view/" in a["href"]]
        if not links:
            break
        pages = page
        current_year = None
        for tag in soup.find_all(["div", "h2", "h3", "a"]):
            text = tag.get_text(" ", strip=True)
            if tag.name in ("div", "h2", "h3") and re.fullmatch(r"20\d{2}", text):
                current_year = text
            if tag.name == "a" and "/issue/view/" in tag.get("href", ""):
                match = re.search(r"20(?:2[0-6])", text)
                year = match.group(0) if match else current_year
                if year in years:
                    years[year][requests.compat.urljoin(url, tag["href"])] = text

    def inspect_issue(year, url):
        response = session.get(url, timeout=60, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else soup.get_text(" ", strip=True)[:200]
        articles = {
            requests.compat.urljoin(url, a["href"])
            for a in soup.find_all("a", href=True)
            if re.search(r"/article/view/\d+$", a["href"])
        }
        return year, url, title, articles

    details = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(inspect_issue, year, url) for year, urls in years.items() for url in urls]
        for future in as_completed(futures):
            details.append(future.result())

    article_counts = {}
    english_issues = {}
    for year in years:
        combined = set()
        year_details = [item for item in details if item[0] == year]
        for _, _, _, articles in year_details:
            combined.update(articles)
        article_counts[year] = len(combined)
        english_issues[year] = sum("english" in title.casefold() for _, _, title, _ in year_details)
    print(json.dumps({
        "archive_pages_scanned": pages,
        "issues_by_year": {year: len(urls) for year, urls in years.items()},
        "unique_articles_by_year": article_counts,
        "english_issues_by_year": english_issues,
    }, indent=2))


if __name__ == "__main__":
    main()
