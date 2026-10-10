FROM oven/bun:1.3-alpine
WORKDIR /app
COPY main.ts package.json ./
ENV NODE_ENV=production
EXPOSE 8080
CMD ["bun", "run", "main.ts"]
