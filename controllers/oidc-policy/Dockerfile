FROM oven/bun:1.3-alpine@sha256:5acc90a93e91ff07bf72aa90a7c9f0fa189765aec90b47bdbf2152d2196383c0
WORKDIR /app
COPY main.ts package.json ./
ENV NODE_ENV=production
EXPOSE 8080
CMD ["bun", "run", "main.ts"]
