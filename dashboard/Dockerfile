# syntax=docker/dockerfile:1.9
# Node 24 LTS, pinned. Node 20 reached end of life in April 2026.

FROM node:24.12.0-alpine3.22 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:24.12.0-alpine3.22 AS build
WORKDIR /app
# Inlined at build time, so the read-only public build cannot render a write control at all.
# Anything but an explicit "false" builds read-only: the default fails closed.
ARG NEXT_PUBLIC_READ_ONLY=true
ENV NEXT_PUBLIC_READ_ONLY=$NEXT_PUBLIC_READ_ONLY \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:24.12.0-alpine3.22 AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -g 10001 healops && adduser -u 10001 -G healops -S healops
COPY --from=build --chown=healops:healops /app/.next/standalone ./
COPY --from=build --chown=healops:healops /app/.next/static ./.next/static
USER healops
EXPOSE 3000
CMD ["node", "server.js"]
