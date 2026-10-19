
(el) => {
    if (!el) return { found: false };
    const s = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const truncated =
        (el.scrollWidth > el.clientWidth + 1 && (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.textOverflow === 'ellipsis')) ||
        (el.scrollHeight > el.clientHeight + 1 && (s.overflow === 'hidden' || s.overflowY === 'hidden'));
    const labels = [];
    if (el.id) {
        document.querySelectorAll('label[for="' + el.id + '"]').forEach(lab => {
            labels.push((lab.textContent || '').trim().slice(0, 100));
        });
    }
    return {
        found: true,
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || '').trim().slice(0, 200),
        outerHtml: el.outerHTML.slice(0, 1500),
        styles: {
            color: s.color,
            backgroundColor: s.backgroundColor,
            fontSize: s.fontSize,
            fontWeight: s.fontWeight,
            display: s.display,
            visibility: s.visibility,
            opacity: s.opacity,
            position: s.position,
            zIndex: s.zIndex,
            overflow: s.overflow,
            textOverflow: s.textOverflow,
            cursor: s.cursor,
            border: s.border,
            padding: s.padding,
            margin: s.margin
        },
        rect: {
            x: rect.x, y: rect.y,
            width: rect.width, height: rect.height,
            inViewport: rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth
        },
        truncated: truncated,
        scrollDimensions: { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight },
        ariaLabel: el.getAttribute('aria-label'),
        ariaDescribedby: el.getAttribute('aria-describedby'),
        ariaHidden: el.getAttribute('aria-hidden'),
        role: el.getAttribute('role'),
        title: el.title || null,
        disabled: !!el.disabled,
        readonly: !!el.readOnly,
        labels: labels,
        focused: document.activeElement === el
    };
}
