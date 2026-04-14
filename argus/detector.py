from __future__ import annotations

import re
from typing import Dict, List, Optional

from .models import Bug, BugType, PageState, Severity


# Suspicious text patterns: (regex, title, severity, description)
_TEXT_PATTERNS = [
    (
        r"\bLoading\.{2,3}\b",
        "Eternal loading indicator",
        Severity.MEDIUM,
        "Page shows 'Loading...' which may indicate content failed to load",
    ),
    (
        r"(?<![A-Za-z\"'\(])NaN(?![A-Za-z_\-\"'\),])",
        "NaN displayed to user",
        Severity.HIGH,
        "NaN (Not a Number) visible on page, indicating a calculation error",
    ),
    (
        r"(?<![\"'(\w])\d+\.\d+\s+days?\s+ago(?![\"'\)])",
        "Broken date formatting",
        Severity.MEDIUM,
        "Date displays decimal in time-ago format (e.g. '1.0 days ago' instead of '1 day ago')",
    ),
]


class Detector:
    """Detects bugs from browser errors, page content, and state changes."""

    def __init__(self):
        self._seen: set[str] = set()
        self._page_counts: Dict[str, int] = {}  # key -> count of pages affected

    # ── Existing: console & network errors ────────────────────────────

    def process_console_errors(
        self, errors: List[Dict], url: str, steps: List[str]
    ) -> List[Bug]:
        bugs = []
        # Separate warnings from errors/exceptions for aggregation
        warnings = [e for e in errors if e["type"] == "warning"]
        non_warnings = [e for e in errors if e["type"] != "warning"]

        # Errors and exceptions: report individually (deduplicated)
        for err in non_warnings:
            key = f"console:{err['text'][:200]}"
            if key in self._seen:
                continue
            self._seen.add(key)
            severity = Severity.HIGH if err["type"] == "exception" else Severity.MEDIUM
            bugs.append(Bug(
                type=BugType.CONSOLE_ERROR,
                severity=severity,
                title=f"Console {err['type']}: {err['text'][:80]}",
                description=err["text"],
                url=url,
                steps_to_reproduce=list(steps),
                console_logs=[err["text"]],
                raw_error=err["text"],
            ))

        # Warnings: aggregate by pattern (strip URLs to group similar warnings)
        if warnings:
            from collections import Counter
            pattern_counts: Counter = Counter()
            pattern_example: Dict[str, str] = {}
            url_re = re.compile(r'https?://\S+')
            for w in warnings:
                # Normalize: replace URLs with [URL] to group same-pattern warnings
                pattern = url_re.sub("[URL]", w["text"])[:80]
                pattern_counts[pattern] += 1
                if pattern not in pattern_example:
                    pattern_example[pattern] = w["text"]

            for pattern, count in pattern_counts.items():
                key = f"console_warn:{pattern}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                example = pattern_example[pattern]
                title = f"Console warning: {example[:80]}" if count == 1 else f"{count} console warnings: {pattern[:70]}..."
                bugs.append(Bug(
                    type=BugType.CONSOLE_ERROR,
                    severity=Severity.LOW,
                    title=title,
                    description=f"{count} warning(s). Example: {example[:200]}",
                    url=url,
                    steps_to_reproduce=list(steps),
                    console_logs=[example],
                ))

        return bugs

    def process_network_errors(
        self, errors: List[Dict], url: str, steps: List[str]
    ) -> List[Bug]:
        bugs = []
        for err in errors:
            key = f"network:{err['method']}:{err['url']}:{err['status']}"
            if key in self._seen:
                continue
            self._seen.add(key)
            severity = Severity.HIGH if err["status"] >= 500 else Severity.MEDIUM
            bugs.append(Bug(
                type=BugType.NETWORK_ERROR,
                severity=severity,
                title=f"HTTP {err['status']} — {err['method']} {err['url'][:60]}",
                description=f"{err['method']} {err['url']} returned {err['status']}",
                url=url,
                steps_to_reproduce=list(steps),
                network_logs=[err],
            ))
        return bugs

    # ── NEW: Page content analysis ────────────────────────────────────

    def process_page_content(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Scan page text for suspicious patterns (broken dates, NaN, eternal loading)."""
        bugs = []
        if not state.page_text:
            return bugs
        for pattern, title, severity, description in _TEXT_PATTERNS:
            match = re.search(pattern, state.page_text, re.IGNORECASE)
            if match:
                key = f"text:{title}:{state.url}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                bugs.append(Bug(
                    type=BugType.TEXT_ANOMALY,
                    severity=severity,
                    title=f"{title}",
                    description=f"{description}. Found: '{match.group()[:100]}'",
                    url=state.url,
                    steps_to_reproduce=list(steps),
                ))
        return bugs

    # ── NEW: Count consistency ────────────────────────────────────────

    def process_count_consistency(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Compare displayed counts against actual item counts on the page."""
        bugs = []
        if not state.counts or not state.item_lists:
            return bugs
        for label, displayed in state.counts.items():
            label_lower = label.lower()
            if not any(w in label_lower for w in ["total", "task", "item", "result"]):
                continue
            for _list_key, items in state.item_lists.items():
                actual = len(items)
                if actual >= 2 and abs(displayed - actual) >= 1:
                    key = f"count:{label}:{displayed}vs{actual}"
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.COUNT_MISMATCH,
                        severity=Severity.MEDIUM,
                        title=f"Count mismatch: '{label}' shows {displayed} but {actual} items visible",
                        description=(
                            f"The displayed count '{displayed} {label}' does not match "
                            f"the {actual} items actually rendered on the page."
                        ),
                        url=state.url,
                        steps_to_reproduce=list(steps),
                    ))
        return bugs

    # ── NEW: CSS state indicators ─────────────────────────────────────

    def process_css_indicators(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Detect problematic CSS states (e.g., alarming red on a success state)."""
        bugs = []
        for indicator in state.css_indicators:
            cls, text = indicator.split(":", 1) if ":" in indicator else (indicator, "")
            if cls == "remaining-zero" and "0" in text:
                key = f"css:{cls}:{state.url}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                bugs.append(Bug(
                    type=BugType.UX_ISSUE,
                    severity=Severity.LOW,
                    title="Completion shown as error state",
                    description=(
                        "0 remaining tasks displayed with alarming red styling. "
                        "This should be a success/celebration state, not an error."
                    ),
                    url=state.url,
                    steps_to_reproduce=list(steps),
                ))
        return bugs

    # ── NEW: Toast + network cross-check ──────────────────────────────

    def process_toast_network_crosscheck(
        self,
        toast_messages: List[str],
        network_errors: List[Dict],
        url: str,
        steps: List[str],
    ) -> List[Bug]:
        """Detect misleading success: toast says 'Saved!' but server returned 500."""
        bugs = []
        success_keywords = ["saved", "deleted", "updated", "created", "success", "added"]
        has_success_toast = any(
            any(kw in toast.lower() for kw in success_keywords)
            for toast in toast_messages
        )
        server_errors = [e for e in network_errors if e["status"] >= 500]
        if has_success_toast and server_errors:
            error_details = [
                f"{e['method']} {e['url'][:60]} -> {e['status']}"
                for e in server_errors
            ]
            toast_text = "; ".join(toast_messages)
            key = f"misleading:{toast_text[:100]}:{url}"
            if key not in self._seen:
                self._seen.add(key)
                bugs.append(Bug(
                    type=BugType.MISLEADING_SUCCESS,
                    severity=Severity.HIGH,
                    title=f"Misleading success: UI says '{toast_text[:50]}' but server returned error",
                    description=(
                        f"The UI showed a success message ('{toast_text}') but the server "
                        f"returned errors: {'; '.join(error_details)}. "
                        f"The user thinks the operation succeeded when it actually failed."
                    ),
                    url=url,
                    steps_to_reproduce=list(steps),
                    network_logs=server_errors,
                ))
        return bugs

    # ── NEW: State verification ───────────────────────────────────────

    @staticmethod
    def _text_present_in_state(text: str, state: PageState) -> bool:
        """Check if text exists anywhere in the page — page_text, elements, or item_lists."""
        text_lower = text.lower().strip()
        if not text_lower:
            return False
        # Check page_text
        if text_lower in state.page_text.lower():
            return True
        # Check element text/value (catches CSS-truncated items)
        for el in state.elements:
            if el.text and text_lower in el.text.lower():
                return True
            if el.value and text_lower in el.value.lower():
                return True
        # Check item_lists
        for items in state.item_lists.values():
            for item in items:
                if text_lower in item.lower():
                    return True
        return False

    def process_state_verification(
        self,
        action_type: str,
        target_text: str,
        before_state: PageState,
        after_state: PageState,
        steps: List[str],
    ) -> List[Bug]:
        """After delete/edit + refresh, verify the action actually persisted."""
        bugs = []

        if action_type == "delete" and target_text:
            if target_text.strip() and self._text_present_in_state(target_text, after_state):
                key = f"verify:delete:{target_text[:80]}"
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.STATE_VERIFICATION,
                        severity=Severity.HIGH,
                        title=f"Delete failed silently: '{target_text[:40]}' still present",
                        description=(
                            f"Deleted item '{target_text}' but it reappeared after page "
                            f"reload. The delete operation did not persist."
                        ),
                        url=after_state.url,
                        steps_to_reproduce=list(steps) + [
                            "Refresh the page",
                            "Item is still present",
                        ],
                    ))

        elif action_type == "edit" and target_text:
            if target_text.strip() and not self._text_present_in_state(target_text, after_state):
                key = f"verify:edit:{target_text[:80]}"
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.STATE_VERIFICATION,
                        severity=Severity.HIGH,
                        title=f"Edit failed silently: '{target_text[:40]}' not found after refresh",
                        description=(
                            f"Edited item to '{target_text}' and saw a success message, "
                            f"but after reloading the new value is not present."
                        ),
                        url=after_state.url,
                        steps_to_reproduce=list(steps) + [
                            "Refresh the page",
                            "Old value is still shown",
                        ],
                    ))

        return bugs

    # ── Dead link crawling ────────────────────────────────────────────

    def process_dead_links(
        self, link_results: List[Dict], url: str, steps: List[str]
    ) -> List[Bug]:
        """Report dead links, aggregated by domain to avoid spam."""
        bugs = []
        dead_links = [r for r in link_results if not r.get("ok")]
        if not dead_links:
            return bugs

        # Group by domain to aggregate
        from urllib.parse import urlparse
        by_domain: Dict[str, list] = {}
        for link in dead_links:
            domain = urlparse(link["href"]).netloc
            by_domain.setdefault(domain, []).append(link)

        for domain, links in by_domain.items():
            key = f"deadlink:{domain}:{url}"
            if key in self._seen:
                continue
            self._seen.add(key)

            statuses = set(l.get("status", 0) for l in links)
            all_connection_failed = statuses == {0}

            if len(links) == 1:
                link = links[0]
                status = link.get("status", 0)
                severity = Severity.MEDIUM if status == 0 else (Severity.HIGH if status >= 500 else Severity.MEDIUM)
                bugs.append(Bug(
                    type=BugType.BROKEN_LINK,
                    severity=severity,
                    title=f"Dead link: {link['href'][:80]} (HTTP {status})",
                    description=f"Link to {link['href']} returned HTTP {status}",
                    url=url, steps_to_reproduce=list(steps),
                ))
            else:
                # Multiple dead links on same domain — aggregate
                severity = Severity.LOW if all_connection_failed else Severity.MEDIUM
                example = links[0]["href"][:80]
                bugs.append(Bug(
                    type=BugType.BROKEN_LINK,
                    severity=severity,
                    title=f"{len(links)} dead links on {domain}",
                    description=f"{len(links)} links to {domain} failed (status: {', '.join(str(s) for s in statuses)}). Example: {example}",
                    url=url, steps_to_reproduce=list(steps),
                ))
        return bugs

    # ── Broken images ─────────────────────────────────────────────────

    def process_broken_images(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Detect images that failed to load."""
        bugs = []
        for img in state.images:
            src = img.get("src", "")
            if not src:
                continue
            if img.get("complete") and not img.get("loaded"):
                key = f"broken_img:{src[:200]}"
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.BROKEN_IMAGE,
                        severity=Severity.MEDIUM,
                        title=f"Broken image: {src[:80]}",
                        description=f"Image at {src} failed to load",
                        url=state.url,
                        steps_to_reproduce=list(steps),
                    ))
        return bugs

    # ── SEO & meta audit ──────────────────────────────────────────────

    def process_seo(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Check for essential SEO/meta tags and heading hierarchy."""
        bugs = []
        meta = state.meta_tags
        if not meta:
            return bugs

        for field, title in [
            ("title", "Missing page title"),
            ("description", "Missing meta description"),
            ("viewport", "Missing viewport meta tag"),
        ]:
            if not meta.get(field):
                key = f"seo:{field}"
                self._page_counts[key] = self._page_counts.get(key, 0) + 1
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.SEO_ISSUE, severity=Severity.MEDIUM,
                        title=title,
                        description=f"First found on {state.url}",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))

        missing_og = [f for f in ["ogTitle", "ogDescription", "ogImage"] if not meta.get(f)]
        if missing_og:
            og_key = f"seo:og:{','.join(sorted(missing_og))}"
            self._page_counts[og_key] = self._page_counts.get(og_key, 0) + 1
            if og_key not in self._seen:
                self._seen.add(og_key)
                bugs.append(Bug(
                    type=BugType.SEO_ISSUE, severity=Severity.LOW,
                    title=f"Missing Open Graph tags: {', '.join(missing_og)}",
                    description=f"First found on {state.url}",
                    url=state.url, steps_to_reproduce=list(steps),
                ))

        headings = state.headings
        if headings:
            h1_count = sum(1 for h in headings if h.get("level") == 1)
            if h1_count == 0:
                key = "seo:no_h1"
                self._page_counts[key] = self._page_counts.get(key, 0) + 1
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.SEO_ISSUE, severity=Severity.MEDIUM,
                        title="No H1 heading on page",
                        description=f"First found on {state.url}",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))
            elif h1_count > 1:
                key = f"seo:multi_h1:{state.url}"
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.SEO_ISSUE, severity=Severity.LOW,
                        title=f"Multiple H1 headings ({h1_count})",
                        description=f"Page has more than one H1 tag",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))

        return bugs

    # ── Accessibility ─────────────────────────────────────────────────

    def process_accessibility(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Check basic accessibility issues, aggregated by type per page."""
        bugs = []
        if not state.accessibility_issues:
            return bugs

        # Aggregate by type to avoid reporting 30 identical issues
        from collections import Counter
        type_counts: Counter = Counter()
        type_examples: Dict[str, str] = {}
        for issue in state.accessibility_issues:
            itype = issue.get("type", "")
            type_counts[itype] += 1
            if itype not in type_examples:
                if itype == "img_no_alt":
                    type_examples[itype] = issue.get("src", "")[:80]
                elif itype == "input_no_label":
                    type_examples[itype] = f"{issue.get('tag', 'input')}[type={issue.get('inputType', '?')}]"
                elif itype == "no_accessible_name":
                    type_examples[itype] = issue.get("html", "")[:80]

        for itype, count in type_counts.items():
            if itype == "no_html_lang":
                key = "a11y:no_lang"
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.ACCESSIBILITY, severity=Severity.MEDIUM,
                        title="Missing lang attribute on <html>",
                        description="The <html> element has no lang attribute",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))

            elif itype == "input_no_label":
                key = "a11y:no_label"
                self._page_counts[key] = self._page_counts.get(key, 0) + 1
                if key not in self._seen:
                    self._seen.add(key)
                    example = type_examples.get(itype, "")
                    bugs.append(Bug(
                        type=BugType.ACCESSIBILITY, severity=Severity.MEDIUM,
                        title=f"Form input(s) without label",
                        description=f"Form elements have no associated label or aria-label. Example: {example}. First found on {state.url}",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))

            elif itype == "no_accessible_name":
                key = "a11y:no_name"
                self._page_counts[key] = self._page_counts.get(key, 0) + 1
                if key not in self._seen:
                    self._seen.add(key)
                    example = type_examples.get(itype, "")
                    bugs.append(Bug(
                        type=BugType.ACCESSIBILITY, severity=Severity.MEDIUM,
                        title=f"Interactive element(s) with no accessible name",
                        description=f"Buttons/links have no text, aria-label, or title. Example: {example}. First found on {state.url}",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))

            elif itype == "img_no_alt":
                key = "a11y:img_no_alt"
                self._page_counts[key] = self._page_counts.get(key, 0) + 1
                if key not in self._seen:
                    self._seen.add(key)
                    example = type_examples.get(itype, "")
                    bugs.append(Bug(
                        type=BugType.ACCESSIBILITY, severity=Severity.INFO,
                        title=f"Image(s) missing alt text",
                        description=f"Images have no alt attribute. Example: {example}. First found on {state.url}",
                        url=state.url, steps_to_reproduce=list(steps),
                    ))

        return bugs

    # ── Performance ───────────────────────────────────────────────────

    def process_performance(
        self, perf_data: Dict, url: str, steps: List[str]
    ) -> List[Bug]:
        """Flag slow page loads, large resources, excessive requests."""
        bugs = []
        nav = perf_data.get("navigation")
        summary = perf_data.get("summary", {})

        if nav:
            load_time = nav.get("loadTime", 0)
            if load_time > 3000:
                key = f"perf:slow:{url}"
                if key not in self._seen:
                    self._seen.add(key)
                    bugs.append(Bug(
                        type=BugType.PERFORMANCE,
                        severity=Severity.MEDIUM if load_time < 5000 else Severity.HIGH,
                        title=f"Slow page load: {load_time/1000:.1f}s",
                        description=f"Page took {load_time/1000:.1f}s to load. TTFB: {nav.get('ttfb',0)/1000:.1f}s",
                        url=url, steps_to_reproduce=list(steps),
                    ))

        if summary.get("totalRequests", 0) > 50:
            key = "perf:too_many_requests"
            self._page_counts[key] = self._page_counts.get(key, 0) + 1
            if key not in self._seen:
                self._seen.add(key)
                bugs.append(Bug(
                    type=BugType.PERFORMANCE, severity=Severity.LOW,
                    title=f"Too many network requests ({summary['totalRequests']} on this page)",
                    description=f"Pages are making >50 network requests. First found on {url}",
                    url=url, steps_to_reproduce=list(steps),
                ))

        for res in perf_data.get("resources", []):
            size_mb = res.get("size", 0) / (1024 * 1024)
            key = f"perf:large:{res['name'][:100]}"
            if key not in self._seen:
                self._seen.add(key)
                bugs.append(Bug(
                    type=BugType.PERFORMANCE, severity=Severity.LOW,
                    title=f"Large resource ({size_mb:.1f}MB): {res['name'][:60]}",
                    description=f"Resource {res['name']} is {size_mb:.1f}MB",
                    url=url, steps_to_reproduce=list(steps),
                ))

        return bugs

    # ── Mixed content ─────────────────────────────────────────────────

    def process_mixed_content(
        self, state: PageState, steps: List[str]
    ) -> List[Bug]:
        """Flag HTTP resources loaded on HTTPS pages."""
        bugs = []
        if not state.url.startswith("https://"):
            return bugs
        for item in state.mixed_content:
            key = f"mixed:{item.get('url', '')[:200]}"
            if key not in self._seen:
                self._seen.add(key)
                bugs.append(Bug(
                    type=BugType.MIXED_CONTENT, severity=Severity.HIGH,
                    title=f"Mixed content: HTTP <{item.get('tag', '?')}> on HTTPS page",
                    description=f"<{item.get('tag', '?')} {item.get('attr', 'src')}=\"{item.get('url', '')}\"> loads over HTTP",
                    url=state.url, steps_to_reproduce=list(steps),
                ))
        return bugs
