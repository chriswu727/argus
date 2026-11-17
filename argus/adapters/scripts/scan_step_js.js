() => {
    // One step of the search for text a lazy or virtualized list has not
    // rendered yet. Scrolls the window, then each scrollable box (open shadow
    // roots included), by less than its own height so no row is stepped over.
    // Returns false once nothing is left to scroll. Evaluated by Argus and by
    // exported tests, so it must stay self-contained.
    const step = (size) => Math.max(100, Math.floor(size * 0.9));
    const page = document.scrollingElement || document.documentElement;
    const before = window.scrollY;
    if (window.innerHeight + before < page.scrollHeight - 2) {
        // 'instant' because a page with smooth scrolling would not have moved yet.
        window.scrollBy({ top: step(window.innerHeight), behavior: 'instant' });
        if (window.scrollY !== before) return true;
    }
    const boxes = [];
    const visit = (root) => {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) visit(el.shadowRoot);
            // A textarea's content is its value, which the collector reads whole.
            if (el.tagName === 'TEXTAREA' || el.clientHeight < 40) continue;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 2) continue;
            const flow = getComputedStyle(el).overflowY;
            if (flow === 'auto' || flow === 'scroll' || flow === 'overlay') boxes.push(el);
        }
    };
    visit(document);
    for (const el of boxes) {
        const top = el.scrollTop;
        el.scrollTo({ top: top + step(el.clientHeight), behavior: 'instant' });
        if (el.scrollTop !== top) return true;
    }
    return false;
}
