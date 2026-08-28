FROM docker.io/library/node:22.17.1-bookworm-slim@sha256:ffb27ca0f26a231a08930c872631cea70cbb318463d1e712922b5c7cfdc3fcca AS dependencies

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

FROM docker.io/library/node:22.17.1-bookworm-slim@sha256:ffb27ca0f26a231a08930c872631cea70cbb318463d1e712922b5c7cfdc3fcca AS builder

WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
ARG NEXT_PUBLIC_VIEWER_API_BASE_URL=/api/viewer
ARG NEXT_PUBLIC_VIEWER_ENVIRONMENT=gcp-dev
ENV NEXT_PUBLIC_VIEWER_API_BASE_URL=${NEXT_PUBLIC_VIEWER_API_BASE_URL}
ENV NEXT_PUBLIC_VIEWER_ENVIRONMENT=${NEXT_PUBLIC_VIEWER_ENVIRONMENT}

COPY --from=dependencies /app/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY next.config.mjs postcss.config.mjs tsconfig.json ./
COPY src ./src

RUN npm run build

FROM docker.io/library/node:22.17.1-bookworm-slim@sha256:ffb27ca0f26a231a08930c872631cea70cbb318463d1e712922b5c7cfdc3fcca AS runtime

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3100

COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

USER node

EXPOSE 3100

CMD ["node", "server.js"]
