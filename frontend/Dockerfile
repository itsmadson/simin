# Simin frontend.
#
# Next.js standalone output: the build stage produces a self-contained server
# bundle with only the modules actually reached, so the runtime image does not
# carry node_modules at all.

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund


FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Baked in at build time, because NEXT_PUBLIC_* is inlined into the client
# bundle rather than read at runtime. Override with a build arg when the API
# is not reachable at localhost:8000 from the browser.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

RUN npm run build


FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

RUN useradd --create-home --uid 10002 simin
USER simin

COPY --from=build --chown=simin:simin /app/.next/standalone ./
COPY --from=build --chown=simin:simin /app/.next/static ./.next/static
COPY --from=build --chown=simin:simin /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
