# Background workspace image: a headless browser + shell that the Daimon
# daemon runs one instance of per active task/project, invisible to the
# user's own screen.
FROM node:22-slim

WORKDIR /workspace

COPY agents/package.json agents/package-lock.json ./
RUN npm ci
RUN npx playwright install --with-deps chromium

# Source changes are far more frequent than dependency changes — copy it
# last so editing agents/src doesn't invalidate the (slow) install layers.
COPY agents/tsconfig.json ./
COPY agents/src ./src

ENV PORT=4711
EXPOSE 4711

CMD ["npm", "start"]
