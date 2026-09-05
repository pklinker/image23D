# Vite bakes VITE_* env vars into the JS bundle at build time, so the API's
# base URL has to be a build ARG here rather than a runtime env var like every
# other service in this stack. Changing it means rebuilding this image -- a
# known SPA static-build limitation, not something to solve with a proxy or a
# runtime config.js shim until this actually needs to run against more than
# one API origin.
FROM node:22-slim AS build
WORKDIR /app
COPY viewer/package.json viewer/package-lock.json ./
RUN npm ci
COPY viewer /app
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
