
({query, kind}) => {
    const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const needle = norm(query);
    const words = needle.split(' ').filter(Boolean);
    const visible = el => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
        if (r.width <= 0 || r.height <= 0) return false;
        try { if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false; } catch (_) {}
        return true;
    };
    const kindMatches = el => {
        if (!kind) return true;
        if (kind === 'heading') return /^H[1-6]$/.test(el.tagName) || el.getAttribute('role') === 'heading';
        if (kind === 'image') return el.tagName === 'IMG' || el.tagName === 'SVG' || el.getAttribute('role') === 'img';
        return true;
    };
    const roots = [document];
    const nodes = [];
    for (let i = 0; i < roots.length; i++) {
        for (const el of roots[i].querySelectorAll('*')) {
            nodes.push(el);
            if (el.shadowRoot) roots.push(el.shadowRoot);
        }
    }
    const ignored = new Set(['HTML', 'BODY', 'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK']);
    const candidates = [];
    for (const el of nodes) {
        if (ignored.has(el.tagName) || !visible(el) || !kindMatches(el)) continue;
        const text = norm(el.innerText || el.textContent);
        const directText = norm(Array.from(el.childNodes).filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join(' '));
        const aria = norm(el.getAttribute('aria-label'));
        const alt = norm(el.getAttribute('alt'));
        const title = norm(el.getAttribute('title'));
        const id = norm(el.id);
        const name = norm(el.getAttribute('name'));
        const faces = [directText, text, aria, alt, title].filter(Boolean);
        let score = 0;
        if (directText === needle) score = 140;
        else if (text === needle) score = 130;
        else if (aria === needle || alt === needle || title === needle) score = 125;
        else if (id === needle || name === needle) score = 100;
        else if (faces.some(face => face.includes(needle))) score = 82;
        else if (words.length && words.every(word => faces.some(face => face.includes(word)))) score = 62;
        if (!score) continue;
        if (/^H[1-6]$/.test(el.tagName) || ['IMG', 'P', 'LABEL'].includes(el.tagName)) score += 8;
        if (text.length > Math.max(needle.length * 5, 160)) score -= 15;
        const rect = el.getBoundingClientRect();
        candidates.push({el, score, text, aria, alt, rect});
    }
    candidates.sort((a, b) => b.score - a.score || a.text.length - b.text.length || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
    const summarize = c => ({
        tag: c.el.tagName.toLowerCase(), score: c.score,
        text: (c.text || c.aria || c.alt || '').slice(0, 120),
        rect: {x: c.rect.x, y: c.rect.y, width: c.rect.width, height: c.rect.height}
    });
    if (!candidates.length || candidates[0].score < 45) return {found: false, candidates: candidates.slice(0, 5).map(summarize)};
    if (candidates[1] && candidates[1].score >= candidates[0].score - 3) {
        return {found: false, ambiguous: true, candidates: candidates.slice(0, 5).map(summarize)};
    }
    const el = candidates[0].el;
    const s = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const truncated =
        (el.scrollWidth > el.clientWidth + 1 && (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.textOverflow === 'ellipsis')) ||
        (el.scrollHeight > el.clientHeight + 1 && (s.overflow === 'hidden' || s.overflowY === 'hidden'));
    const labels = [];
    if (el.id) document.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]').forEach(label => labels.push(norm(label.textContent).slice(0, 100)));
    return {
        found: true, matchScore: candidates[0].score,
        tag: el.tagName.toLowerCase(), text: (el.textContent || '').trim().slice(0, 200),
        outerHtml: el.outerHTML.slice(0, 1500),
        styles: {
            color: s.color, backgroundColor: s.backgroundColor, fontSize: s.fontSize,
            fontWeight: s.fontWeight, display: s.display, visibility: s.visibility,
            opacity: s.opacity, position: s.position, zIndex: s.zIndex,
            overflow: s.overflow, textOverflow: s.textOverflow, cursor: s.cursor,
            border: s.border, padding: s.padding, margin: s.margin
        },
        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
               inViewport: rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth},
        truncated,
        scrollDimensions: {scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight},
        ariaLabel: el.getAttribute('aria-label'), ariaDescribedby: el.getAttribute('aria-describedby'),
        ariaHidden: el.getAttribute('aria-hidden'), role: el.getAttribute('role'), title: el.title || null,
        disabled: !!el.disabled, readonly: !!el.readOnly, labels,
        focused: document.activeElement === el
    };
}
