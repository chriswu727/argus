(row) => {
    // A row's text as Argus records it (parent_context) and as exported tests
    // match it: its textContent without any <textarea>'s content, which is a
    // value (possibly a secret, and stale once edited), not row text. Shared
    // verbatim by element extraction and exported tests, so keep it
    // self-contained.
    const doc = row.ownerDocument;
    const view = doc.defaultView;
    const walker = doc.createTreeWalker(row, 4 /* NodeFilter.SHOW_TEXT */);
    const inline = (el) => {
        try { return view.getComputedStyle(el).display === 'inline'; } catch (e) { return true; }
    };
    let text = '';
    let last = null;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentNode;
        if (!parent || String(parent.nodeName).toUpperCase() === 'TEXTAREA') continue;
        // Markup without whitespace between elements (what most frameworks
        // emit) would otherwise glue a label to the button beside it into
        // one word that matches neither.
        if (last && parent !== last && text && !(inline(parent) && inline(last))) text += ' ';
        text += node.nodeValue;
        last = parent;
    }
    return text;
}
