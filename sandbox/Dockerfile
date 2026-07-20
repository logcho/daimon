# Background workspace image: a headless browser + shell that the Daimon
# daemon runs one instance of per active task/project, invisible to the
# user's own screen.
FROM node:22-slim

WORKDIR /workspace

COPY agents/package.json agents/package-lock.json ./
RUN npm ci

COPY agents/tsconfig.json ./
COPY agents/src ./src

RUN npx playwright install --with-deps chromium

ENV PORT=4711
EXPOSE 4711

CMD ["npm", "start"]
