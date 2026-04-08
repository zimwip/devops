# Multi-stage: build React → serve with nginx
FROM node:20-alpine AS builder
WORKDIR /app

# Build args must be declared in the stage that uses them
ARG APP_VERSION=0.1.0
ARG GIT_COMMIT=local
ARG BUILD_DATE=unknown
# Vite exposes VITE_* vars to the bundle at build time
ENV VITE_APP_VERSION=${APP_VERSION} \
    VITE_GIT_COMMIT=${GIT_COMMIT} \
    VITE_BUILD_DATE=${BUILD_DATE}

COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
ARG APP_VERSION=0.1.0
ARG GIT_COMMIT=local
LABEL version="${APP_VERSION}" commit="${GIT_COMMIT}"

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -qO- http://localhost/health || exit 1
CMD ["nginx", "-g", "daemon off;"]
