
() => {
    const result = { navigation: null, resources: [], summary: {} };
    try {
        const nav = performance.getEntriesByType('navigation');
        if (nav.length > 0) {
            const n = nav[0];
            result.navigation = {
                loadTime: n.loadEventEnd - n.startTime,
                domContentLoaded: n.domContentLoadedEventEnd - n.startTime,
                ttfb: n.responseStart - n.startTime,
                domInteractive: n.domInteractive - n.startTime
            };
        }
    } catch(e) {}
    try {
        const resources = performance.getEntriesByType('resource');
        result.summary.totalRequests = resources.length;
        result.summary.totalSize = 0;
        resources.forEach(r => {
            const sz = r.transferSize || 0;
            result.summary.totalSize += sz;
            if (sz > 500 * 1024) result.resources.push({name:r.name.slice(0,200),type:r.initiatorType,size:sz,duration:Math.round(r.duration)});
        });
    } catch(e) {}
    return result;
}
