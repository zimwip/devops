/**
 * driftChecker.groovy — Scheduled drift detection and POC expiry pipeline.
 *
 * Runs every 15 minutes via Jenkins cron trigger.
 * Delegates to platform/scripts/drift_checker.py which:
 *   - Compares desired state (envs/{env}/{service}/version.yaml) vs actual cluster
 *   - Sends Slack notifications on drift
 *   - Auto-remediates on configured environments (default: dev)
 *   - Warns 6h before POC expiry, tears down at expiry
 *
 * Required Jenkins credentials:
 *   - platform-config-repo   : URL of the platform-config git repo
 *   - slack-webhook-url      : Slack incoming webhook URL
 *
 * Required Jenkins global env vars:
 *   - SLACK_CHANNEL          : Default alert channel (e.g. #platform-alerts)
 *   - AUTO_REMEDIATE_ENVS    : Comma-separated envs to auto-remediate (e.g. dev)
 */
pipeline {
    agent {
        kubernetes {
            yaml """
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: python
    image: python:3.12-slim
    command: [sleep, infinity]
    resources:
      requests: {cpu: 200m, memory: 256Mi}
      limits:   {cpu: 500m, memory: 512Mi}
  - name: kubectl
    image: bitnami/kubectl:latest
    command: [sleep, infinity]
    resources:
      requests: {cpu: 100m, memory: 128Mi}
      limits:   {cpu: 200m, memory: 256Mi}
"""
        }
    }

    triggers {
        // Run every 15 minutes
        cron('H/15 * * * *')
    }

    options {
        timeout(time: 10, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '48'))  // keep 12h of runs
        disableConcurrentBuilds()
    }

    environment {
        SLACK_WEBHOOK_URL  = credentials('slack-webhook-url')
        SLACK_CHANNEL      = "${env.SLACK_CHANNEL ?: '#platform-alerts'}"
        AUTO_REMEDIATE_ENVS = "${env.AUTO_REMEDIATE_ENVS ?: 'dev'}"
    }

    stages {
        stage('Checkout platform-config') {
            steps {
                dir('platform-config') {
                    git url: env.PLATFORM_CONFIG_REPO,
                        credentialsId: 'github-token',
                        branch: 'main'
                }
            }
        }

        stage('Install dependencies') {
            steps {
                container('python') {
                    sh """
                        pip install --quiet pyyaml requests
                    """
                }
            }
        }

        stage('Drift + expiry check') {
            steps {
                container('python') {
                    dir('platform-config') {
                        sh """
                            PLATFORM_CONFIG_DIR=\$(pwd) \\
                            SLACK_WEBHOOK_URL="${env.SLACK_WEBHOOK_URL}" \\
                            SLACK_CHANNEL="${env.SLACK_CHANNEL}" \\
                            AUTO_REMEDIATE_ENVS="${env.AUTO_REMEDIATE_ENVS}" \\
                            python3 scripts/drift_checker.py
                        """
                    }
                }
            }
            post {
                // Exit code 1 = drift detected (unstable, not failure)
                // Exit code 2 = error (failure)
                unstable { echo "Drift detected in one or more environments." }
                failure  { echo "Drift checker encountered an unexpected error." }
            }
        }
    }

    post {
        always {
            echo "Drift check finished: ${currentBuild.result ?: 'SUCCESS'} — ${new Date()}"
        }
    }
}
