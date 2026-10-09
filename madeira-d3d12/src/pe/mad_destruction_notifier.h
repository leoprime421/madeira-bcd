/* Optional destruction notifications share the owner's COM lifetime.
 * The registry owns the sidecar, but never an owner reference: registrations
 * must not keep the object alive. Every exported notifier reference DOES hold
 * an owner reference. Dispatch before private data and implicit dependencies
 * are released, with no registry lock held while calling application code. */
#ifndef MAD_DESTRUCTION_NOTIFIER_H
#define MAD_DESTRUCTION_NOTIFIER_H
#include <d3dcommon.h>
#include <stdlib.h>
#include <stdint.h>

struct mad_destruction_callback {
    struct mad_destruction_callback *next;
    PFN_DESTRUCTION_CALLBACK fn;
    void *data;
    UINT id;
};
struct mad_destruction_notifier {
    ID3DDestructionNotifier iface;
    IUnknown *owner;
    struct mad_destruction_notifier *next;
    struct mad_destruction_callback *callbacks;
    uint64_t next_id;
};
static SRWLOCK mad_notifier_lock = SRWLOCK_INIT;
static struct mad_destruction_notifier *mad_notifiers[256];
static unsigned mad_notifier_hash(const void *p) {
    uintptr_t x = (uintptr_t)p;
    return (unsigned)((x >> 4) ^ (x >> 12)) & 255;
}
static struct mad_destruction_notifier *mad_notifier_impl(ID3DDestructionNotifier *iface) {
    return CONTAINING_RECORD(iface, struct mad_destruction_notifier, iface);
}
static HRESULT STDMETHODCALLTYPE mad_notifier_qi(ID3DDestructionNotifier *iface, REFIID iid, void **out) {
    return IUnknown_QueryInterface(mad_notifier_impl(iface)->owner, iid, out);
}
static ULONG STDMETHODCALLTYPE mad_notifier_addref(ID3DDestructionNotifier *iface) {
    return IUnknown_AddRef(mad_notifier_impl(iface)->owner);
}
static ULONG STDMETHODCALLTYPE mad_notifier_release(ID3DDestructionNotifier *iface) {
    /* Release may destroy this sidecar. Do not access it afterwards. */
    return IUnknown_Release(mad_notifier_impl(iface)->owner);
}
static HRESULT STDMETHODCALLTYPE mad_notifier_register(ID3DDestructionNotifier *iface,
        PFN_DESTRUCTION_CALLBACK fn, void *data, UINT *id) {
    struct mad_destruction_notifier *n = mad_notifier_impl(iface);
    struct mad_destruction_callback *c;
    if (!id) return E_POINTER;
    *id = 0;
    if (!fn) return E_INVALIDARG;
    c = malloc(sizeof(*c));
    if (!c) return E_OUTOFMEMORY;
    c->fn = fn; c->data = data;
    AcquireSRWLockExclusive(&mad_notifier_lock);
    /* Never reuse an ID, including after unregister and at UINT wrap. */
    if (n->next_id > UINT32_MAX) {
        ReleaseSRWLockExclusive(&mad_notifier_lock);
        free(c); return E_OUTOFMEMORY;
    }
    c->id = (UINT)n->next_id++;
    c->next = n->callbacks; n->callbacks = c;
    *id = c->id;
    ReleaseSRWLockExclusive(&mad_notifier_lock);
    return S_OK;
}
static HRESULT STDMETHODCALLTYPE mad_notifier_unregister(ID3DDestructionNotifier *iface, UINT id) {
    struct mad_destruction_notifier *n = mad_notifier_impl(iface);
    struct mad_destruction_callback **p, *c;
    AcquireSRWLockExclusive(&mad_notifier_lock);
    for (p = &n->callbacks; *p && (*p)->id != id; p = &(*p)->next) {}
    c = *p;
    if (c) *p = c->next;
    ReleaseSRWLockExclusive(&mad_notifier_lock);
    if (!c) return E_INVALIDARG;
    free(c);
    return S_OK;
}
static const ID3DDestructionNotifierVtbl mad_notifier_vtbl = {
    mad_notifier_qi, mad_notifier_addref, mad_notifier_release,
    mad_notifier_register, mad_notifier_unregister
};
static HRESULT mad_notifier_get(IUnknown *owner, void **out) {
    struct mad_destruction_notifier *n;
    unsigned h = mad_notifier_hash(owner);
    if (!out) return E_POINTER;
    *out = NULL;
    AcquireSRWLockExclusive(&mad_notifier_lock);
    for (n = mad_notifiers[h]; n && n->owner != owner; n = n->next) {}
    if (!n) {
        n = calloc(1, sizeof(*n));
        if (!n) { ReleaseSRWLockExclusive(&mad_notifier_lock); return E_OUTOFMEMORY; }
        n->iface.lpVtbl = &mad_notifier_vtbl;
        n->owner = owner; n->next_id = 1;
        n->next = mad_notifiers[h]; mad_notifiers[h] = n;
    }
    /* Caller already holds a live owner reference. */
    IUnknown_AddRef(owner);
    *out = &n->iface;
    ReleaseSRWLockExclusive(&mad_notifier_lock);
    return S_OK;
}
static void mad_notifier_destroy(const void *owner) {
    struct mad_destruction_notifier **p, *n;
    struct mad_destruction_callback *c, *next;
    unsigned h = mad_notifier_hash(owner);
    AcquireSRWLockExclusive(&mad_notifier_lock);
    for (p = &mad_notifiers[h]; *p && (*p)->owner != owner; p = &(*p)->next) {}
    n = *p;
    if (n) *p = n->next;
    ReleaseSRWLockExclusive(&mad_notifier_lock);
    if (!n) return;
    for (c = n->callbacks; c; c = next) {
        next = c->next;
        c->fn(c->data);
        free(c);
    }
    free(n);
}
#endif
