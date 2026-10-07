// Login for the bot dashboard (nginx njs). Replaces the browser's basic-auth dialog
// with the page in site/login.html and a signed session cookie.
//
//   POST /auth/login   {"user","password","remember"} → {"ok":true} + dash_session cookie,
//                      or {"ok":false}; 429 when rate-limited
//   GET  /auth/logout  clears the cookie, back to /
//   /auth/check        auth_request subrequest for every other URL: 204 / 401
//
// Cookie: dash_session=<expiry unix s>.<HMAC-SHA256(expiry)>, HttpOnly, SameSite=Lax,
// Secure behind HTTPS. The key is derived from DASHBOARD_USER + DASHBOARD_PASSWORD, so it
// is the same in every nginx worker and changing the password logs everyone out.

var crypto = require('crypto');

var COOKIE = 'dash_session';
var DAY = 86400;

function key() {
    return crypto.createHash('sha256')
        .update('bot-dashboard|' + (process.env.DASHBOARD_USER || '') + '|' +
                (process.env.DASHBOARD_PASSWORD || ''))
        .digest('hex');
}

function sign(value) {
    return crypto.createHmac('sha256', key()).update(value).digest('hex');
}

// Compare through an HMAC so the time taken doesn't depend on where strings differ
function same(a, b) {
    return sign('cmp|' + a) === sign('cmp|' + b);
}

function valid(cookie) {
    var parts = String(cookie || '').split('.');
    if (parts.length !== 2 || !/^[0-9]{1,12}$/.test(parts[0])) {
        return false;
    }
    if (Number(parts[0]) <= Date.now() / 1000) {
        return false;
    }
    return same(sign(parts[0]), parts[1]);
}

function secureFlag(r) {
    return r.headersIn['X-Forwarded-Proto'] === 'https' ? '; Secure' : '';
}

function check(r) {
    r.return(valid(r.variables['cookie_' + COOKIE]) ? 204 : 401);
}

function login(r) {
    r.headersOut['Content-Type'] = 'application/json';
    r.headersOut['Cache-Control'] = 'no-store';
    if (r.method !== 'POST') {
        r.return(405, '{"ok":false}');
        return;
    }
    var body;
    try {
        body = JSON.parse(r.requestText || '{}');
    } catch (e) {
        r.return(400, '{"ok":false}');
        return;
    }
    var user = process.env.DASHBOARD_USER || '';
    var password = process.env.DASHBOARD_PASSWORD || '';
    // Both compared every time (no early exit on a wrong user name)
    var userOk = same(String(body.user || ''), user);
    var passOk = same(String(body.password || ''), password);
    if (!password || !userOk || !passOk) {
        // 200 with ok:false, not 401: nginx's "error_page 401" would replace the answer
        r.return(200, '{"ok":false}');
        return;
    }
    var days = body.remember ? 30 : 1;
    var expiry = String(Math.floor(Date.now() / 1000) + days * DAY);
    r.headersOut['Set-Cookie'] = COOKIE + '=' + expiry + '.' + sign(expiry) +
        '; Path=/; HttpOnly; SameSite=Lax; Max-Age=' + (days * DAY) + secureFlag(r);
    r.return(200, '{"ok":true}');
}

function logout(r) {
    r.headersOut['Set-Cookie'] = COOKIE + '=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0' +
        secureFlag(r);
    r.return(302, '/');
}

export default { check: check, login: login, logout: logout, _valid: valid, _sign: sign };
