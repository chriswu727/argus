
() => {
    const sel = 'a, button, input, select, textarea, summary, [contenteditable="true"], [contenteditable=""], [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="switch"], [role="combobox"], [role="textbox"], [role="slider"], [role="spinbutton"], [role="option"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="gridcell"], [onclick], [tabindex]:not([tabindex="-1"]), [draggable="true"]';
    const MAX = 400;
    const out = [];
    const seen = new Set();

    // Structural path for a form field with no identifying attribute. Its name
    // may come from a nearby <label> that no attribute selector can express, so
    // without this the field could be listed but never typed into.
    function cssPath(el) {
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && node.tagName !== 'HTML') {
            if (node.id) {
                parts.unshift('[id="' + node.id.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]');
                break;
            }
            let i = 1, sib = node;
            while ((sib = sib.previousElementSibling)) if (sib.tagName === node.tagName) i++;
            parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
            node = node.parentElement;
        }
        return parts.join(' > ');
    }

    function record(el, inShadow, frameSel) {
        if (seen.has(el)) return;
        seen.add(el);
        const rect = el.getBoundingClientRect();
        const style = (el.ownerDocument.defaultView || window).getComputedStyle(el);
        // Invisible-to-a-human elements must not appear as interactive. Use
        // checkVisibility so an ANCESTOR's opacity:0 / visibility:hidden hides the
        // whole subtree (opacity is not inherited, so a button inside an
        // opacity:0 fade-menu has its own opacity 1 and used to leak through).
        // Plus zero-size and off-the-left-edge (left:-9999px "sr-only" hack).
        // EXCEPTION: checkbox/radio/file inputs are overwhelmingly restyled with
        // opacity:0 behind a visible label/graphic (TodoMVC, Bootstrap, Material,
        // custom file uploads) yet fully functional — don't let opacity hide them,
        // or their whole widget becomes untargetable.
        const _t = (el.type || '').toLowerCase();
        const _skipOpacity = el.tagName.toLowerCase() === 'input' &&
                             (_t === 'checkbox' || _t === 'radio' || _t === 'file');
        const _vis = el.checkVisibility
            ? el.checkVisibility({opacityProperty: !_skipOpacity, visibilityProperty: true, contentVisibilityAuto: true})
            : (style.display !== 'none' && style.visibility !== 'hidden' && (_skipOpacity || style.opacity !== '0'));
        if (!_vis || rect.width === 0 || rect.height === 0 || rect.right <= 0) return;
        const doc = el.ownerDocument;
        if (!_skipOpacity) {
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            if (cx >= 0 && cx < doc.defaultView.innerWidth && cy >= 0 && cy < doc.defaultView.innerHeight) {
                const root = el.getRootNode();
                const hitRoot = root && root.elementFromPoint ? root : doc;
                const top = hitRoot.elementFromPoint(cx, cy);
                if (top && top !== el && !el.contains(top)) return;
            }
        }
        // role=gridcell is broad (data grids AND calendar days). Keep only the
        // CLICKABLE ones (cursor:pointer) so a date-picker's day cells — which use
        // the roving-tabindex pattern (only the selected day is tabindex=0) — are
        // targetable, without flooding observe with a data grid's display cells.
        if (el.getAttribute('role') === 'gridcell' && style.cursor !== 'pointer') return;
        out.push({
            index: out.length,
            tag: el.tagName.toLowerCase(),
            // Tag a contenteditable (rich-text editor) as a text-entry surface so
            // type_into (kind=input) can target it, not just click_what.
            type: el.type || (el.isContentEditable ? 'contenteditable' : null),
            // A <select>'s textContent mashes every option together
            // ("Name (A to Z)Name (Z to A)Price...") — noise. Leave text null so
            // the label (aria/associated-<label>) is the name, and put the
            // SELECTED option's visible text in value below. Result:
            // labelled -> `select "Sort" = "Name Z-A"`, unlabelled -> the
            // selection is the label: `select "Name (A to Z)"`.
            text: (el.tagName === 'SELECT') ? null
                  : ((el.textContent || '').trim().slice(0, 100) || null),
            // aria-placeholder / data-placeholder cover contenteditable rich-text
            // editors (Quill, ProseMirror, Draft) whose empty-state hint isn't a
            // real <input> placeholder — otherwise the editor is an unlabelled div.
            placeholder: el.placeholder || el.getAttribute('aria-placeholder') || el.getAttribute('data-placeholder') || null,
            href: el.href || null,
            // For a select, value = the SELECTED option's visible text (not its
            // internal value attr "az"), so describe shows the human-readable
            // current choice.
            value: (el.tagName === 'SELECT')
                   ? (((el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0].text : el.value) || '').trim().slice(0, 80) || null)
                   : (el.value || null),
            // Live checked state for checkbox/radio (and role=switch/checkbox via
            // aria-checked) — without it, a checked and unchecked box look
            // identical in observe. null for non-checkable elements.
            checked: (el.type === 'checkbox' || el.type === 'radio') ? el.checked
                     : (el.getAttribute('aria-checked') === 'true' ? true
                        : (el.getAttribute('aria-checked') === 'false' ? false : null)),
            // Compact ARIA STATE — is a dropdown/accordion open (expanded), a
            // toggle button pressed, a nav item current? All invisible otherwise:
            // a collapsed and an expanded disclosure button look identical.
            aria_state: (function(){
                var s = [];
                var e = el.getAttribute('aria-expanded');
                if (e === 'true') s.push('expanded'); else if (e === 'false') s.push('collapsed');
                // Native <details>/<summary> uses the `open` property, not
                // aria-expanded — surface the same expanded/collapsed signal.
                else if (el.tagName === 'SUMMARY' && el.parentElement && el.parentElement.tagName === 'DETAILS')
                    s.push(el.parentElement.open ? 'expanded' : 'collapsed');
                if (el.getAttribute('aria-pressed') === 'true') s.push('pressed');
                var c = el.getAttribute('aria-current');
                if (c && c !== 'false') s.push('current');
                if (el.getAttribute('aria-selected') === 'true') s.push('selected');
                // Validation error: explicit aria-invalid, OR native HTML5
                // :invalid but ONLY when the field has a value (a filled-but-bad
                // field is the signal; an empty required field is not yet a bug).
                var inv = el.getAttribute('aria-invalid');
                if (inv === 'true') s.push('invalid');
                else { try { if (el.value && el.matches && el.matches(':invalid')) s.push('invalid'); } catch(_) {} }
                return s.length ? s.join(' ') : null;
            })(),
            disabled: el.disabled || false,
            role: el.getAttribute('role') || null,
            // Fall back to the field's VISIBLE label (a person targets a form
            // field by the label they see next to it). Prefer the accessible
            // name (label[for], wrapping <label>, aria-labelledby); then the
            // common unassociated preceding <label> sibling. The missing `for`
            // is still flagged separately as an a11y issue.
            aria_label: el.getAttribute('aria-label') || (function(){
                var t = el.tagName.toLowerCase();
                if (t !== 'input' && t !== 'select' && t !== 'textarea') return null;
                var pick = function(n){ var s = n && n.textContent ? n.textContent.trim() : ''; return s ? s.slice(0,100) : null; };
                if (el.id) { try { var lf = doc.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]'); if (pick(lf)) return pick(lf); } catch (e) {} }
                var w = el.closest('label'); if (pick(w)) return pick(w);
                var lb = el.getAttribute('aria-labelledby'); if (lb) { var t2 = doc.getElementById(lb); if (pick(t2)) return pick(t2); }
                var p = el.previousElementSibling;
                while (p) { if (p.tagName === 'LABEL' && pick(p)) return pick(p); if (p.tagName === 'INPUT' || p.tagName === 'SELECT' || p.tagName === 'TEXTAREA') break; p = p.previousElementSibling; }
                // Custom combobox (react-select, Downshift, etc.): the visible
                // placeholder / selected value lives in a control container div,
                // not a <label>, so an unlabelled combobox input is otherwise
                // untargetable. Use the control's visible text ("Select...", or
                // the current value like "Ocean").
                if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox') {
                    var ctrl = el.closest('[class*="control"], [class*="combobox"], [class*="Select"]');
                    if (ctrl && ctrl !== el) { var ct = ctrl.textContent ? ctrl.textContent.trim() : ''; if (ct) return ct.slice(0, 100); }
                }
                return null;
            })() || el.getAttribute('title') || null,
            name: el.name || null,
            id: el.id || null,
            parent_context: (el.closest('li, tr, .card, .list-item, [class*="item"], [class*="row"]') || {}).textContent?.trim()?.slice(0, 200) || null,
            shadow: inShadow,
            frame: frameSel || null,
            css_path: (!inShadow && ['input', 'select', 'textarea'].includes(el.tagName.toLowerCase()) &&
                       !el.id && !el.name && !el.getAttribute('placeholder') && !el.getAttribute('aria-label'))
                ? cssPath(el) : null,
        });
    }

    // A selector for an iframe element, usable by Playwright frame_locator.
    // JSON.stringify handles the quoting/escaping (no manual regex/backslashes).
    function ifSel(ifr, i) {
        if (ifr.id) return 'iframe[id=' + JSON.stringify(ifr.id) + ']';
        if (ifr.name) return 'iframe[name=' + JSON.stringify(ifr.name) + ']';
        var src = ifr.getAttribute('src'); if (src) return 'iframe[src=' + JSON.stringify(src) + ']';
        return 'iframe >> nth=' + i;
    }

    function walk(root, inShadow, frameSel) {
        if (out.length >= MAX) return;
        root.querySelectorAll(sel).forEach(el => { if (out.length < MAX) record(el, inShadow, frameSel); });
        // Descend into open shadow roots hosted anywhere under this root.
        root.querySelectorAll('*').forEach(node => {
            if (out.length < MAX && node.shadowRoot) walk(node.shadowRoot, true, frameSel);
        });
        // Descend into iframes. Same-origin: walk the frame document, tagging its
        // elements with the iframe selector CHAIN (so actions can frame_locator
        // into it). Cross-origin: contentDocument throws — surface a marker so
        // the agent knows an untestable embedded region exists instead of being
        // silently blind to it.
        root.querySelectorAll('iframe').forEach(function(ifr, i) {
            if (out.length >= MAX) return;
            var chain = frameSel ? (frameSel + ' >>> ' + ifSel(ifr, i)) : ifSel(ifr, i);
            var fdoc = null;
            try { fdoc = ifr.contentDocument; } catch (e) { fdoc = null; }
            if (fdoc && fdoc.documentElement) {
                walk(fdoc, inShadow, chain);
            } else {
                var r = ifr.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    out.push({ index: out.length, tag: 'iframe', type: null,
                        text: '[embedded region (' + (ifr.getAttribute('title') || ifr.getAttribute('name') || ifr.getAttribute('src') || 'iframe') + ') — contents not readable here]',
                        placeholder: null, href: ifr.getAttribute('src') || null, value: null,
                        disabled: false, role: null, aria_label: ifr.getAttribute('title') || null,
                        name: ifr.getAttribute('name') || null, id: ifr.id || null,
                        parent_context: null, shadow: inShadow, frame: null });
                }
            }
        });
    }

    walk(document, false, null);
    return out;
}
