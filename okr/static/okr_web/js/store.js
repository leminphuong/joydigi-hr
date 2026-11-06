(function registerStore(globalScope) {
  'use strict';

  const namespace = globalScope.JoyOKR = globalScope.JoyOKR || {};
  const events = new EventTarget();
  let state = {
    route: globalScope.JoyOKRConfig?.initialRoute || 'dashboard',
    query: {},
    user: globalScope.JoyOKRConfig?.currentUser || null,
    cache: new Map(),
  };

  /** Returns an immutable snapshot of current application state. */
  function getState() {
    return { ...state, query: { ...state.query }, cache: new Map(state.cache) };
  }

  /** Merges state and announces the changed keys. */
  function setState(update) {
    const changedKeys = Object.keys(update);
    state = { ...state, ...update };
    events.dispatchEvent(new CustomEvent('change', { detail: { changedKeys, state: getState() } }));
  }

  /** Stores a short-lived API response under a stable key. */
  function cache(key, value) {
    const nextCache = new Map(state.cache);
    nextCache.set(key, { value, storedAt: Date.now() });
    setState({ cache: nextCache });
  }

  /** Clears all cache entries or those starting with a prefix. */
  function invalidate(prefix = '') {
    const nextCache = new Map(state.cache);
    for (const key of nextCache.keys()) if (!prefix || key.startsWith(prefix)) nextCache.delete(key);
    setState({ cache: nextCache });
  }

  function subscribe(listener) {
    const handler = (event) => listener(event.detail);
    events.addEventListener('change', handler);
    return () => events.removeEventListener('change', handler);
  }

  namespace.store = Object.freeze({ getState, setState, cache, invalidate, subscribe });
})(globalThis);
