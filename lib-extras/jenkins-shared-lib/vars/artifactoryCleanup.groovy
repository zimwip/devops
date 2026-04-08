/**
 * artifactoryCleanup.groovy — Weekly Artifactory artifact retention policy pipeline.
 *
 * Runs every Sunday at 2am via Jenkins cron trigger.
 * Delegates to platform/scripts/cleanup.py which:
 *   - Builds a deployed-version guardrail (never deletes anything currently running)
 *   - Purges snapshots older than 7 days / more than 10 per service
 *   - Purges RC artifacts older than 30 days
 *   - Purges release artifacts older than 90 days / more than 10 per service
 *   - Skips POC artifacts (managed by drift_checker.py TTL teardown)
 *
 * Required Jenkins credentials:
 *   - artifactory-url          : Artifactory base URL
 *   - artifactory-credentials  : Username + password/API key
 *   - platform-config-repo     : URL of the platform-config git repo
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
  - name: helm
    image: alpine/helm:latest
    command: [sleep, infinity]
    resources:
      requests: {cpu: 100m, memory: 128Mi}
      limits:   {cpu: 200m, memory: 256Mi}
"""
        }
    }

    triggers {
        // Sunday at 2am
        cron('H 2 * * 0')
    }

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '12'))  // keep ~3 months of weekly runs
        disableConcurrentBuilds()
    }

    parameters {
        booleanParam(
            name: 'DRY_RUN',
            defaultValue: false,
            description: 'Log what would be deleted without actually deleting anything'
        )
    }

    environment {
        ARTIFACTORY_URL   = credentials('artifactory-url')
        ARTIFACTORY_CREDS = credentials('artifactory-credentials')
        DRY_RUN           = "${params.DRY_RUN}"
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

        stage('Get deployed versions (guardrail)') {
            steps {
                container('helm') {
                    // Export helm list output for the cleanup script to consume
                    sh """
                        helm list --all-namespaces --output json > /tmp/helm-releases.json 2>/dev/null || echo '[]' > /tmp/helm-releases.json
                        echo "Found \$(cat /tmp/helm-releases.json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') deployed releases"
                    """
                }
            }
        }

        stage('Enforce retention policy') {
            steps {
                container('python') {
                    dir('platform-config') {
                        sh """
                            PLATFORM_CONFIG_DIR=\$(pwd) \\
                            ARTIFACTORY_URL="${env.ARTIFACTORY_URL}" \\
                            ARTIFACTORY_USER="${env.ARTIFACTORY_CREDS_USR}" \\
                            ARTIFACTORY_PASS="${env.ARTIFACTORY_CREDS_PSW}" \\
                            DRY_RUN="${env.DRY_RUN}" \\
                            python3 scripts/cleanup.py ${params.DRY_RUN ? '--dry-run' : ''}
                        """
                    }
                }
            }
        }
    }

    post {
        success {
            echo "Artifactory cleanup completed successfully — ${new Date()}"
        }
        failure {
            echo "Artifactory cleanup FAILED — check logs for details"
        }
    }
}
