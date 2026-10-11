FROM oven/bun:1.4-alpine@sha256:629e17411f1f129dbec3af78d5af9c9f2a937435c80349437206c6b0b7422373
WORKDIR /app
COPY main.ts package.json ./
ENV NODE_ENV=production
EXPOSE 8080
CMD ["bun", "run", "main.ts"]
