// Selector engine "argus": resolves a reference written by the element
// extractor to the node it was read from. A removed node matches nothing, so an
// action on a stale reference fails instead of landing on a neighbour.
({
    query(root, selector) {
        return this.queryAll(root, selector)[0] || null;
    },
    queryAll(root, selector) {
        const doc = root.ownerDocument || root;
        const store = doc.defaultView && doc.defaultView.__argusRefs;
        const cut = selector.lastIndexOf(':');
        const nodes = store && store.get(selector.slice(0, cut));
        const el = nodes && nodes[Number(selector.slice(cut + 1))];
        return el && el.isConnected && el.ownerDocument === doc ? [el] : [];
    }
})
