package com.myorg.platform;

/**
 * Stub platform library — testenv placeholder for the real lib-platform artifact.
 *
 * In production this library provides: security filters, structured logging setup,
 * OpenTelemetry auto-configuration, Kafka producer/consumer utilities, and shared
 * Spring Boot auto-configuration.
 *
 * This stub satisfies compile-time dependencies in the testenv without pulling
 * in the full implementation.
 */
public class PlatformAutoConfiguration {
    private PlatformAutoConfiguration() {}
}
