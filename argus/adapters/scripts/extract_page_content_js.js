
() => {
    const result = { pageText: '', toasts: [], counts: {}, cssIndicators: [], itemLists: {},
                     links: [], images: [], metaTags: {}, headings: [], a11yIssues: [], mixedContent: [],
                     openModals: [], focused: null, viewport: null, canvases: [] };

    // Canvas regions — pixel-drawn UI (charts/dashboards, maps, whiteboards,
    // PDF.js, WebGL games) is invisible to DOM extraction. Surface each visible
    // canvas with its on-screen rect so the agent knows to SCREENSHOT it and
    // reach it with coordinate actions (click_at / type_at / hover_at).
    try {
        document.querySelectorAll('canvas').forEach(c => {
            const r = c.getBoundingClientRect();
            const st = window.getComputedStyle(c);
            if (r.width >= 40 && r.height >= 40 && st.display !== 'none' && st.visibility !== 'hidden') {
                result.canvases.push({
                    x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
                    left: Math.round(r.left), top: Math.round(r.top),
                    w: Math.round(r.width), h: Math.round(r.height),
                    id: c.id || null, label: c.getAttribute('aria-label') || c.title || null
                });
            }
        });
    } catch(e) {}

    // 1. Full visible text — simple and robust
    try {
        result.pageText = (document.body.innerText || '').slice(0, 5000);
    } catch(e) {}

    // 1a. Open modals / dialogs / popovers
    try {
        const modalSels = '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open], .modal.show, .modal.open';
        document.querySelectorAll(modalSels).forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') return;
            const text = (el.textContent || '').trim().slice(0, 300);
            if (!text) return;
            result.openModals.push({
                role: el.getAttribute('role') || el.tagName.toLowerCase(),
                ariaLabel: el.getAttribute('aria-label') || '',
                ariaLabelledby: el.getAttribute('aria-labelledby') || '',
                text: text,
                isModal: el.getAttribute('aria-modal') === 'true'
            });
        });
    } catch(e) {}

    // 1b. What's focused right now?
    try {
        const f = document.activeElement;
        if (f && f !== document.body && f.tagName !== 'BODY') {
            result.focused = {
                tag: f.tagName.toLowerCase(),
                type: f.type || null,
                id: f.id || null,
                name: f.name || null,
                ariaLabel: f.getAttribute('aria-label') || null,
                text: (f.textContent || '').trim().slice(0, 80) || null,
                placeholder: f.placeholder || null,
                value: (typeof f.value === 'string' ? f.value.slice(0, 80) : null)
            };
        }
    } catch(e) {}

    // 1c. Viewport vs document height — let the agent know if there's content below the fold
    try {
        result.viewport = {
            scrollY: window.scrollY,
            scrollX: window.scrollX,
            innerHeight: window.innerHeight,
            innerWidth: window.innerWidth,
            documentHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
            documentWidth: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),
            atTop: window.scrollY <= 1,
            atBottom: (window.scrollY + window.innerHeight) >= (Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - 1)
        };
    } catch(e) {}

    // 2. Toast/notification messages
    try {
        const sels = '.toast, [role="alert"], .alert-success, .alert-error, .alert-warning, [class*="toast"]';
        document.querySelectorAll(sels).forEach(el => {
            const text = el.textContent.trim();
            if (text) {
                const s = window.getComputedStyle(el);
                result.toasts.push({
                    text: text.slice(0, 200),
                    visible: s.display !== 'none' && s.visibility !== 'hidden',
                    classes: el.className || ''
                });
            }
        });
    } catch(e) {}

    // 3. Number + label counts
    try {
        document.querySelectorAll('.stat, .stat-val, .count, .badge, h1, h2, h3, p, span').forEach(el => {
            const text = el.textContent.trim();
            const m = text.match(/^(\d+)\s+(.+)$/);
            if (m) result.counts[m[2].trim()] = parseInt(m[1], 10);
        });
    } catch(e) {}

    // 4. Semantic CSS indicators
    try {
        ['remaining-zero','task-done','error','loading','spinner','alert-error','alert-success'].forEach(cls => {
            document.querySelectorAll('.' + cls).forEach(el => {
                result.cssIndicators.push({
                    cls: cls,
                    text: el.textContent.trim().slice(0, 100),
                    tag: el.tagName.toLowerCase()
                });
            });
        });
    } catch(e) {}

    // 5. Item lists — report only rows a human can SEE. The page-text,
    // interactive-element, and counts extractors all skip hidden nodes; this
    // one used to not, so after a client-side hide (optimistic UI / fake-delete)
    // it leaked display:none / visibility:hidden rows — content no one sees, and
    // self-contradictory with the same payload's page-text. It also feeds the
    // reproduction receipt (_text_in_state), so a stale-invisible row could taint
    // verification. Filter both the container and its items.
    try {
        const _shown = (el) => {
            const s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        document.querySelectorAll('.card, .list, ul, ol').forEach(container => {
            if (!_shown(container)) return;
            const items = Array.from(container.querySelectorAll('.task-item, .list-item, li, tr')).filter(_shown);
            if (items.length >= 2) {
                const key = (container.className || container.tagName).slice(0, 50);
                result.itemLists[key] = items.map(it => it.textContent.trim().slice(0, 200));
            }
        });
    } catch(e) {}

    // 6. All links on page
    try {
        document.querySelectorAll('a[href]').forEach(el => {
            const href = el.href;
            if (!href || href.startsWith('javascript:') || href === '#') return;
            result.links.push({
                href: href,
                text: (el.textContent || '').trim().slice(0, 100),
                isInternal: href.startsWith(window.location.origin)
            });
        });
    } catch(e) {}

    // 7. Images
    try {
        document.querySelectorAll('img').forEach(el => {
            result.images.push({
                src: el.src || el.getAttribute('src') || '',
                alt: el.alt,
                hasAlt: el.hasAttribute('alt'),
                naturalWidth: el.naturalWidth,
                naturalHeight: el.naturalHeight,
                complete: el.complete,
                loaded: el.complete && el.naturalWidth > 0
            });
        });
    } catch(e) {}

    // 8. Meta tags & headings (SEO)
    try {
        const gm = (n) => { const e = document.querySelector('meta[name="'+n+'"], meta[property="'+n+'"]'); return e ? (e.content||'') : ''; };
        result.metaTags = {
            title: document.title || '',
            description: gm('description'),
            ogTitle: gm('og:title'),
            ogDescription: gm('og:description'),
            ogImage: gm('og:image'),
            canonical: (document.querySelector('link[rel="canonical"]') || {}).href || '',
            viewport: gm('viewport'),
            htmlLang: document.documentElement.lang || ''
        };
        document.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(el => {
            result.headings.push({ level: parseInt(el.tagName[1]), text: el.textContent.trim().slice(0, 200) });
        });
    } catch(e) {}

    // 9. Accessibility basics
    try {
        document.querySelectorAll('img').forEach(el => {
            if (!el.hasAttribute('alt')) {
                result.a11yIssues.push({type:'img_no_alt', src: (el.src||'').slice(0,100)});
            }
        });
        document.querySelectorAll('input,select,textarea').forEach(el => {
            if (el.type==='hidden'||el.type==='submit'||el.type==='button') return;
            const s = window.getComputedStyle(el);
            if (s.display==='none'||s.visibility==='hidden') return;
            const has = (el.id && document.querySelector('label[for="'+el.id+'"]')) || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title || el.closest('label');
            if (!has) result.a11yIssues.push({type:'input_no_label', tag:el.tagName.toLowerCase(), inputType:el.type||'', name:el.name||'', placeholder:el.placeholder||''});
        });
        document.querySelectorAll('button, a, [role="button"]').forEach(el => {
            const s = window.getComputedStyle(el);
            if (s.display==='none'||s.visibility==='hidden') return;
            if (!(el.textContent||'').trim() && !el.getAttribute('aria-label') && !el.title && !el.querySelector('img[alt]')) {
                result.a11yIssues.push({type:'no_accessible_name', tag:el.tagName.toLowerCase(), html:el.outerHTML.slice(0,150)});
            }
        });
        if (!document.documentElement.lang) result.a11yIssues.push({type:'no_html_lang'});
    } catch(e) {}

    // 10. Mixed content
    try {
        if (window.location.protocol === 'https:') {
            const ck = (sel, attr) => { document.querySelectorAll(sel).forEach(el => { const u=el.getAttribute(attr)||''; if(u.startsWith('http://')) result.mixedContent.push({url:u.slice(0,200),tag:el.tagName.toLowerCase(),attr:attr}); }); };
            ck('img[src]','src'); ck('script[src]','src'); ck('link[href]','href'); ck('iframe[src]','src'); ck('video[src]','src'); ck('audio[src]','src');
        }
    } catch(e) {}

    return result;
}
