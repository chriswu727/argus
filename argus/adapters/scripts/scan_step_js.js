() => {
    // One step of the search for text a lazy or virtualized list has not
    // rendered yet, in one frame. Scrolls the frame's window, then each
    // scrollable box (open shadow roots included). Returns false once nothing
    // in this frame is left to scroll. Evaluated in every rendered frame by
    // Argus and by exported tests, so it must stay self-contained.
    //
    // A step is never longer than what is visible, or the rows in between
    // would never be rendered: nine tenths of the height, at least one pixel.
    const step = (size) => Math.min(size, Math.max(1, Math.floor(size * 0.9)));
    const page = document.scrollingElement || document.documentElement;
    const before = window.scrollY;
    if (window.innerHeight >= 1 && window.innerHeight + before < page.scrollHeight - 2) {
        // 'instant' because a page with smooth scrolling would not have moved yet.
        window.scrollBy({ top: step(window.innerHeight), behavior: 'instant' });
        if (window.scrollY !== before) return true;
    }
    const boxes = [];
    const visit = (root) => {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) visit(el.shadowRoot);
            // A textarea's or select's content is its value, which the collector
            // reads whole. A box with no height shows nothing to scroll through.
            if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.clientHeight < 1) continue;
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 2) continue;
            const style = getComputedStyle(el);
            if (style.visibility === 'hidden') continue;
            const flow = style.overflowY;
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
