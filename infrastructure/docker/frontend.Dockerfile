# Frontend image: Next.js in production mode.
FROM node:20-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:20-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json
RUN chown -R appuser:appuser /app
USER appuser
EXPOSE 3000
CMD ["npm", "run", "start"]
