# Static bot dashboard: nginx serves index.html plus a read-only copy of the
# bots' SQLite snapshot. The page reads the database in the browser (sql.js),
# so there is no API. Login is a session cookie handled by nginx itself (njs,
# auth.js), shipped with the official image.
FROM nginx:1.27-alpine

COPY nginx-main.conf /etc/nginx/nginx.conf
COPY auth.js /etc/nginx/auth.js
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY 40-htpasswd.sh /docker-entrypoint.d/40-htpasswd.sh
RUN chmod +x /docker-entrypoint.d/40-htpasswd.sh
COPY site /usr/share/nginx/html
