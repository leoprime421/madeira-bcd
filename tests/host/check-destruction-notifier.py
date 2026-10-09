#!/usr/bin/env python3
"""Exercise destruction notifier COM lifetime and callbacks with real Wine headers."""
from pathlib import Path
import os, subprocess, tempfile
root = Path(__file__).resolve().parents[2]
wine = Path(os.environ.get('MADEIRA_WINE_SRC', root / 'wine'))
build = Path(os.environ.get('MADEIRA_WINE_BUILD', wine / 'build-host'))
source = r'''
#define INITGUID
#define COBJMACROS
#include <initguid.h>
#include <windows.h>
#include <assert.h>
#include <pthread.h>
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static void lock_registry(SRWLOCK *unused) { (void)unused; assert(!pthread_mutex_lock(&lock)); }
static void unlock_registry(SRWLOCK *unused) { (void)unused; assert(!pthread_mutex_unlock(&lock)); }
#define AcquireSRWLockExclusive lock_registry
#define ReleaseSRWLockExclusive unlock_registry
#include "mad_destruction_notifier.h"
struct parent { IUnknown iface; unsigned refs, alive, destroyed; };
static ULONG STDMETHODCALLTYPE add(IUnknown *i) { return __atomic_add_fetch(&((struct parent *)i)->refs, 1, __ATOMIC_SEQ_CST); }
static ULONG STDMETHODCALLTYPE release(IUnknown *i) {
    struct parent *p = (struct parent *)i;
    unsigned n = __atomic_sub_fetch(&p->refs, 1, __ATOMIC_SEQ_CST);
    if (!n) {
        mad_notifier_destroy(i);
        assert(p->alive); p->alive=0; ++p->destroyed;
    }
    return n;
}
static HRESULT STDMETHODCALLTYPE query(IUnknown *i, REFIID iid, void **out) {
    if (!out) return E_POINTER;
    *out=NULL;
    if (!iid) return E_INVALIDARG;
    if (IsEqualGUID(iid, &IID_ID3DDestructionNotifier)) return mad_notifier_get(i,out);
    if (IsEqualGUID(iid, &IID_IUnknown)) { *out=i; add(i); return S_OK; }
    return E_NOINTERFACE;
}
static const IUnknownVtbl parent_vtbl = {query, add, release};
static unsigned called;
static struct parent other = {{&parent_vtbl},1,1,0};
static void STDMETHODCALLTYPE callback(void *data) {
    struct parent *p=data;
    ID3DDestructionNotifier *v;
    /* Owner dependencies still alive; callback can enter another notifier. */
    assert(p->alive && !p->destroyed);
    assert(IUnknown_QueryInterface(&other.iface,&IID_ID3DDestructionNotifier,(void **)&v)==S_OK);
    ID3DDestructionNotifier_Release(v);
    __atomic_add_fetch(&called,1,__ATOMIC_SEQ_CST);
}
static void STDMETHODCALLTYPE forbidden(void *data) { (void)data; assert(0); }
struct worker { struct parent *p; UINT ids[100]; };
static void *worker(void *data) {
    struct worker *w=data;
    ID3DDestructionNotifier *v;
    assert(IUnknown_QueryInterface(&w->p->iface,&IID_ID3DDestructionNotifier,(void **)&v)==S_OK);
    for (unsigned i=0;i<100;i++) {
        assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,callback,w->p,&w->ids[i])==S_OK);
        if (!(i%2)) assert(ID3DDestructionNotifier_UnregisterDestructionCallback(v,w->ids[i])==S_OK);
    }
    ID3DDestructionNotifier_Release(v);
    return NULL;
}
int main(void) {
    struct parent p={{&parent_vtbl},1,1,0};
    ID3DDestructionNotifier *v,*v2;
    IUnknown *identity;
    UINT a,b; void *out=(void *)1; GUID unknown={0xdeadbeef};
    assert(mad_notifier_get(&p.iface,NULL)==E_POINTER);
    assert(query(&p.iface,&IID_ID3DDestructionNotifier,(void **)&v)==S_OK);
    assert(ID3DDestructionNotifier_QueryInterface(v,&IID_ID3DDestructionNotifier,(void **)&v2)==S_OK && v2==v);
    assert(ID3DDestructionNotifier_QueryInterface(v,&IID_IUnknown,(void **)&identity)==S_OK && identity==&p.iface);
    assert(ID3DDestructionNotifier_QueryInterface(v,&unknown,&out)==E_NOINTERFACE && !out);
    assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,callback,&p,NULL)==E_POINTER);
    a=99; assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,NULL,&p,&a)==E_INVALIDARG && !a);
    assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,callback,&p,&a)==S_OK);
    assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,forbidden,&p,&b)==S_OK && a!=b);
    assert(ID3DDestructionNotifier_UnregisterDestructionCallback(v,b)==S_OK);
    assert(ID3DDestructionNotifier_UnregisterDestructionCallback(v,b)==E_INVALIDARG);
    IUnknown_Release(identity); ID3DDestructionNotifier_Release(v2); IUnknown_Release(&p.iface);
    assert(p.refs==1 && !called && p.alive);
    ID3DDestructionNotifier_Release(v); /* last notifier ref destroys owner */
    assert(called==1 && p.destroyed==1 && !p.alive);
    /* Reuse the exact address: no stale callbacks or sidecar survives. */
    p.refs=1; p.alive=1; p.destroyed=0;
    pthread_t threads[4]; struct worker workers[4];
    for (unsigned t=0;t<4;t++) { workers[t].p=&p; assert(!pthread_create(&threads[t],NULL,worker,&workers[t])); }
    for (unsigned t=0;t<4;t++) assert(!pthread_join(threads[t],NULL));
    for (unsigned i=0;i<400;i++) for (unsigned j=i+1;j<400;j++)
        assert(workers[i/100].ids[i%100]!=workers[j/100].ids[j%100]);
    assert(p.refs==1 && called==1);
    IUnknown_Release(&p.iface); assert(called==201 && p.destroyed==1);
    /* Registrations survive releasing the notifier, and IDs cannot wrap. */
    p.refs=1;p.alive=1;p.destroyed=0;
    assert(query(&p.iface,&IID_ID3DDestructionNotifier,(void **)&v)==S_OK);
    mad_notifier_impl(v)->next_id=UINT32_MAX;
    assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,callback,&p,&a)==S_OK && a==UINT32_MAX);
    b=7; assert(ID3DDestructionNotifier_RegisterDestructionCallback(v,forbidden,&p,&b)==E_OUTOFMEMORY && !b);
    ID3DDestructionNotifier_Release(v); assert(p.refs==1 && called==201);
    IUnknown_Release(&p.iface); assert(called==202);
    IUnknown_Release(&other.iface);
    for (unsigned i=0;i<256;i++) assert(!mad_notifiers[i]);
    return 0;
}
'''
with tempfile.TemporaryDirectory() as tmp:
    c=Path(tmp)/'notifier.c'; exe=Path(tmp)/'notifier'; c.write_text(source)
    subprocess.run([os.environ.get('CC','cc'),'-O1','-g','-pthread','-fsanitize=address,undefined',
        '-fno-sanitize-recover=undefined','-D__WINESRC__','-include',str(build/'include/config.h'),
        '-I'+str(build/'include'),'-I'+str(wine/'include'),'-I'+str(root/'madeira-d3d12/src/pe'),
        str(c),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
print('PASS: identity, lifetime, cancellation, callback order/reentry, concurrent registrations, ID exhaustion, address reuse')
