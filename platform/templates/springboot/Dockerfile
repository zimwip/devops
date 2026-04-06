# Multi-stage build — keeps the final image lean
FROM eclipse-temurin:21-jdk-alpine AS builder
WORKDIR /build
COPY pom.xml .
COPY src ./src
RUN ./mvnw -B package -DskipTests 2>/dev/null || mvn -B package -DskipTests

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app

# Build args injected by Jenkins
ARG APP_VERSION=0.1.0-SNAPSHOT
ARG GIT_COMMIT=local
ARG BUILD_DATE=unknown

ENV APP_VERSION=${APP_VERSION} \
    GIT_COMMIT=${GIT_COMMIT} \
    BUILD_DATE=${BUILD_DATE}

# OpenTelemetry agent — auto-instruments Spring Boot, Kafka, JDBC
ADD https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar /app/otel-agent.jar

COPY --from=builder /build/target/*.jar app.jar

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://localhost:8080/actuator/health || exit 1

ENTRYPOINT ["java", \
  "-javaagent:/app/otel-agent.jar", \
  "-Dotel.service.name={{SERVICE_NAME}}", \
  "-jar", "app.jar"]
