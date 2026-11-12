(row) => {
    // A row's text as Argus records it (parent_context) and as exported tests
    // match it: its textContent without any <textarea>'s content, which is a
    // value (possibly a secret, and stale once edited), not row text. Shared
    // verbatim by element extraction and exported tests, so keep it
    // self-contained.
    const walker = row.ownerDocument.createTreeWalker(row, 4 /* NodeFilter.SHOW_TEXT */);
    let text = '';
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentNode;
        if (!parent || String(parent.nodeName).toUpperCase() !== 'TEXTAREA') text += node.nodeValue;
    }
    return text;
}
