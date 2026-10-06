# Static bot dashboard: nginx serves index.html plus a read-only copy of the
# bots' SQLite snapshot. The page reads the database in the browser (sql.js),
# so there is no API.
FROM nginx:1.27-alpine

RUN apk add --no-cache openssl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY 40-htpasswd.sh /docker-entrypoint.d/40-htpasswd.sh
RUN chmod +x /docker-entrypoint.d/40-htpasswd.sh
COPY site /usr/share/nginx/html
