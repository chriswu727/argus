() => {
    // One frame's rendered text and controls, for deciding whether a symptom is
    // present. Evaluated once per Playwright frame (cross-origin frames included)
    // by Argus and by exported tests, so it must stay self-contained.
    const LIMIT = 2000000;
    const out = { text: '', fields: [], headings: [], buttons: [], password: false, truncated: false };
    const parts = [];
    let length = 0;
    // Not rendered as text: frames are collected on their own, and a
    // select's or textarea's text is its value, reported as a field.
    const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'TITLE', 'META', 'LINK',
        'IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED', 'CANVAS', 'VIDEO', 'AUDIO', 'DATALIST',
        'OPTION', 'OPTGROUP']);
    const NOT_TEXT_FIELDS = new Set(['hidden', 'password', 'checkbox', 'radio', 'file', 'image',
        'submit', 'button', 'reset', 'range', 'color']);
    const BUTTON_INPUTS = new Set(['submit', 'button', 'reset']);

    const push = (s) => {
        if (out.truncated || !s) return;
        if (length + s.length > LIMIT) {
            parts.push(s.slice(0, LIMIT - length));
            length = LIMIT;
            out.truncated = true;
            return;
        }
        parts.push(s);
        length += s.length;
    };
    const since = (start) => parts.slice(start).join('').replace(/\s+/g, ' ').trim().slice(0, 200);
    const boxed = (el) => el.getClientRects().length > 0;
    const inline = (display) => display.startsWith('inline') || display === 'contents' || display.startsWith('ruby');

    const children = (parent, style) => {
        for (let child = parent.firstChild; child && !out.truncated; child = child.nextSibling) {
            visit(child, style);
        }
    };

    const visit = (node, parentStyle) => {
        if (node.nodeType === 3) {
            // A text node is painted with its parent element's visibility.
            if (!parentStyle || parentStyle.visibility !== 'visible') return;
            const pre = parentStyle.whiteSpace.startsWith('pre') || parentStyle.whiteSpace === 'break-spaces';
            push(pre ? node.data : node.data.replace(/[ \t\n\r\f]+/g, ' '));
            return;
        }
        if (node.nodeType !== 1) return;
        const el = node;
        const tag = el.tagName.toUpperCase();
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.contentVisibility === 'hidden') return;
        const shown = style.visibility === 'visible';
        if (tag === 'INPUT') {
            const type = (el.type || 'text').toLowerCase();
            if (!shown || !boxed(el)) return;
            if (type === 'password') out.password = true;
            if (BUTTON_INPUTS.has(type)) {
                const label = el.value || (type === 'submit' ? 'Submit' : type === 'reset' ? 'Reset' : '');
                push(' ' + label + ' ');
                if (label) out.buttons.push(label.slice(0, 200));
            } else if (type === 'image') {
                const label = el.alt || el.getAttribute('aria-label') || '';
                if (label) out.buttons.push(label.slice(0, 200));
            } else if (!NOT_TEXT_FIELDS.has(type) && el.value) {
                out.fields.push(el.value);
            }
            return;
        }
        if (tag === 'TEXTAREA') {
            if (shown && boxed(el) && el.value) out.fields.push(el.value);
            return;
        }
        if (tag === 'SELECT') {
            if (shown && boxed(el)) {
                for (const option of el.selectedOptions) {
                    const label = (option.label || option.text || '').trim();
                    if (label) out.fields.push(label);
                }
            }
            return;
        }
        if (SKIP.has(tag)) return;
        if (tag === 'BR') {
            if (shown) push('\n');
            return;
        }
        const block = !inline(style.display);
        // An inline-block box (a button, a badge) is drawn apart from its
        // neighbours even with no whitespace between them in the markup.
        const atomic = style.display.startsWith('inline-');
        if (block) push('\n');
        else if (atomic) push(' ');
        const start = parts.length;
        if (el.shadowRoot) {
            children(el.shadowRoot, style);
        } else if (tag === 'SLOT') {
            const assigned = el.assignedNodes();
            if (assigned.length) {
                for (const child of assigned) {
                    if (out.truncated) break;
                    visit(child, style);
                }
            } else {
                children(el, style);
            }
        } else if (tag === 'DETAILS' && !el.open) {
            const summary = Array.from(el.children).find((child) => child.tagName === 'SUMMARY');
            if (summary) visit(summary, style);
        } else {
            children(el, style);
        }
        if (shown) {
            const role = (el.getAttribute('role') || '').toLowerCase();
            if (/^H[1-6]$/.test(tag) || role === 'heading') {
                const heading = since(start);
                if (heading) out.headings.push(heading);
            }
            if (tag === 'BUTTON' || role === 'button') {
                const label = since(start) || el.getAttribute('aria-label') || el.title || '';
                if (label) out.buttons.push(label.slice(0, 200));
            }
        }
        if (block) push('\n');
        else if (atomic) push(' ');
    };

    try {
        const root = document.body || document.documentElement;
        if (root) visit(root, null);
    } catch (e) {
        // A partial walk must not read as a complete one.
        out.truncated = true;
    }
    out.text = parts.join('').replace(/[ \t]*\n[ \t\n]*/g, '\n').replace(/ {2,}/g, ' ').trim();
    return out;
}
