/* Reads the page configuration Django renders with json_script, so the
 * page needs no inline script (the OKR page's CSP forbids them). */
(function (globalScope) {
  'use strict';
  const element = document.getElementById('joyokr-config');
  globalScope.JoyOKRConfig = element ? JSON.parse(element.textContent) : {};
})(globalThis);
