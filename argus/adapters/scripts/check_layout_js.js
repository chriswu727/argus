
() => {
    const norm = value => (value || '').replace(/\s+/g, ' ').trim();
    const describe = (el, rect) => ({
        tag: el.tagName.toLowerCase(),
        label: norm(el.getAttribute('aria-label') || el.innerText || el.textContent || el.getAttribute('alt') || el.id).slice(0, 100),
        rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
    });
    const visible = (el, style, rect) => style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    const overflow = [], clipped = [], smallTargets = [], overlays = [];
    const interactive = 'a[href],button,input:not([type="hidden"]),select,textarea,[role="button"],[role="link"],[tabindex]:not([tabindex="-1"])';
    for (const el of document.body.querySelectorAll('*')) {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        if (!visible(el, style, rect)) continue;
        if ((rect.right > innerWidth + 1 || rect.left < -1) && !['fixed', 'sticky'].includes(style.position)) overflow.push(describe(el, rect));
        const hasText = !!norm(el.innerText || el.textContent);
        const clipsX = el.scrollWidth > el.clientWidth + 1 && ['hidden', 'clip'].includes(style.overflowX);
        const clipsY = el.scrollHeight > el.clientHeight + 1 && ['hidden', 'clip'].includes(style.overflowY);
        if (hasText && (clipsX || clipsY || (style.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1))) clipped.push(describe(el, rect));
        if (el.matches(interactive) && (rect.width < 44 || rect.height < 44)) smallTargets.push(describe(el, rect));
        if (['fixed', 'sticky'].includes(style.position) && rect.bottom > 0 && rect.top < innerHeight) overlays.push(describe(el, rect));
    }
    return {
        viewport: {width: innerWidth, height: innerHeight, devicePixelRatio},
        document: {width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight},
        horizontalOverflow: overflow.slice(0, 20), clippedText: clipped.slice(0, 20),
        smallTargets: smallTargets.slice(0, 20), fixedOrSticky: overlays.slice(0, 20),
        totals: {horizontalOverflow: overflow.length, clippedText: clipped.length, smallTargets: smallTargets.length, fixedOrSticky: overlays.length}
    };
}
