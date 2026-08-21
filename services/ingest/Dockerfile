# --- Build/runtime image for the Ingest Service ---------------------------
# "alpine" is a tiny Linux. A small image downloads faster and has fewer
# programs inside it, which means fewer things an attacker could abuse.
FROM node:20-alpine

# Never run as root inside a container. The node image already ships a
# non-root user called "node" - we use it.
WORKDIR /app

# Copy the dependency list first and install. Docker caches this layer, so
# rebuilding after a code change does not re-download the internet.
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# Now the source code.
COPY server.js zones.js ./

# Drop privileges.
USER node

ENV NODE_ENV=production
ENV PORT=8080
EXPOSE 8080

# tini-style signal handling: node as PID 1 receives SIGTERM directly,
# which our code handles for graceful shutdown.
CMD ["node", "server.js"]
