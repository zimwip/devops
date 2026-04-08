/**
 * buildService.groovy — Shared build pipeline for all microservices.
 *
 * Usage in a service Jenkinsfile:
 *   @Library('platform-shared-lib@v1.0') _
 *   buildService()
 *
 * Build behaviour is driven by .platform/build.yaml in the service repo.
 * Services scaffolded from older templates may still pass a 'template' param
 * as a fallback: buildService(template: 'springboot').
 */
def call(Map config = [:]) {
    def legacyTemplate = config.get('template', 'springboot')
    def buildCfg = [:]   // populated in Version stage from .platform/build.yaml

    // Pod template images — controlled here so service teams cannot override them.
    def agentYaml = """
apiVersion: v1
kind: Pod
spec:
  volumes:
  - name: maven-settings
    configMap:
      name: maven-settings
  - name: platform-scripts
    emptyDir: {}
  containers:
  - name: maven
    image: maven:3.9-eclipse-temurin-17
    command: [sleep, infinity]
    resources:
      requests: {cpu: 500m, memory: 1Gi}
      limits:   {cpu: 2,    memory: 2Gi}
    env:
    - name: ARTIFACTORY_PASSWORD
      valueFrom:
        secretKeyRef:
          name: artifactory-credentials
          key: password
    volumeMounts:
    - name: maven-settings
      mountPath: /root/.m2/settings.xml
      subPath: settings.xml
    - name: platform-scripts
      mountPath: /opt/platform
  - name: node
    image: node:20-alpine
    command: [sleep, infinity]
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 1,    memory: 1Gi}
    volumeMounts:
    - name: platform-scripts
      mountPath: /opt/platform
  - name: python
    image: python:3.12-slim
    command: [sleep, infinity]
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 1,    memory: 1Gi}
    volumeMounts:
    - name: platform-scripts
      mountPath: /opt/platform
  - name: sonar-scanner
    image: sonarsource/sonar-scanner-cli:latest
    command: [sleep, infinity]
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 1,    memory: 1Gi}
    volumeMounts:
    - name: platform-scripts
      mountPath: /opt/platform
  - name: docker
    image: docker:24-dind
    securityContext:
      privileged: true
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 2,    memory: 2Gi}
    volumeMounts:
    - name: platform-scripts
      mountPath: /opt/platform
"""

    pipeline {
        agent {
            kubernetes {
                yaml agentYaml
                defaultContainer 'maven'
            }
        }

        options {
            timeout(time: 20, unit: 'MINUTES')
            buildDiscarder(logRotator(numToKeepStr: '20'))
            disableConcurrentBuilds()
        }

        environment {
            SERVICE_NAME  = "${env.JOB_NAME.split('/')[0]}"
            REGISTRY      = credentials('registry-url')
            GITHUB_TOKEN  = credentials('github-token')
            // Artifactory/Helm credentials are resolved lazily in the stages that
            // need them (Helm package & push, Release Promotion) to avoid failing
            // the pipeline on services that haven't configured these credentials yet.
        }

        stages {

            stage('Setup') {
                steps {
                    script {
                        // Trust the workspace directory regardless of which UID owns it.
                        // git 2.35.2+ rejects repos owned by a different user by default;
                        // in Jenkins Kubernetes pods the workspace volume is mounted as root
                        // while the jnlp agent may run as jenkins (uid 1000).
                        sh "git config --global --add safe.directory '*'"

                        // Clone the platform repo so downstream stages can call
                        // /opt/platform/scripts/platform_cli.py and validate_version.py.
                        // PLATFORM_CONFIG_REPO must be set as a Jenkins global env var
                        // (e.g. http://gitea:3000/ap3/platform-repo.git).
                        def platformRepo = env.PLATFORM_CONFIG_REPO
                        if (platformRepo) {
                            sh """
                                if [ ! -d /opt/platform/.git ]; then
                                    git clone --depth 1 ${platformRepo} /opt/platform
                                fi
                            """
                        } else {
                            echo "PLATFORM_CONFIG_REPO not set — platform scripts unavailable"
                        }
                    }
                }
            }

            stage('Validate Version') {
                steps {
                    // Git commands run in the default jnlp container (always has git).
                    // The python container (python:3.12-slim) does not include git.
                    script {
                        env.GIT_TAG = sh(
                            script: "git tag --points-at HEAD 2>/dev/null | head -1 || true",
                            returnStdout: true
                        ).trim()
                        env.GIT_SHA = sh(
                            script: "git rev-parse --short HEAD",
                            returnStdout: true
                        ).trim()
                    }
                    container('python') {
                        script {
                            if (fileExists('version.txt')) {
                                // New path: version.txt is the single source of truth.
                                // validate_version.py validates coherence AND computes the final tag.
                                def validatorPath = '/opt/platform/scripts/validate_version.py'
                                env.SERVICE_VERSION = sh(
                                    script: """
                                        python3 ${validatorPath} \
                                            --branch "${env.BRANCH_NAME}" \
                                            --tag "${env.GIT_TAG}" \
                                            --build-number "${env.BUILD_NUMBER}" \
                                            --sha "${env.GIT_SHA}"
                                    """,
                                    returnStdout: true
                                ).trim()
                                echo "Version (from version.txt): ${env.SERVICE_VERSION}"
                            } else {
                                // Legacy path: version extracted in the Version stage below.
                                echo "No version.txt found — falling back to legacy version extraction"
                            }
                        }
                    }
                }
            }

            stage('Version') {
                when {
                    // Skip when version.txt already resolved the version
                    expression { !env.SERVICE_VERSION }
                }
                steps {
                    script {
                        // Load build config if present (new template structure)
                        if (fileExists('.platform/build.yaml')) {
                            buildCfg = readYaml(file: '.platform/build.yaml')
                        }

                        def rawVersion = ''
                        if (buildCfg?.version) {
                            container(buildCfg.version.container) {
                                rawVersion = sh(
                                    script: buildCfg.version.command,
                                    returnStdout: true
                                ).trim()
                            }
                        } else if (legacyTemplate == 'springboot') {
                            container('maven') {
                                rawVersion = sh(
                                    script: "mvn help:evaluate -Dexpression=project.version -q -DforceStdout",
                                    returnStdout: true
                                ).trim()
                            }
                        } else if (legacyTemplate == 'react') {
                            container('node') {
                                def pkg = readJSON file: 'package.json'
                                rawVersion = pkg.version
                            }
                        } else if (legacyTemplate == 'python-api') {
                            container('python') {
                                rawVersion = sh(
                                    script: "python -c \"import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])\" 2>/dev/null || grep -m1 '^version' setup.cfg | cut -d= -f2 | tr -d ' '",
                                    returnStdout: true
                                ).trim()
                            }
                        }

                        // Compute versioned tag from raw version + branch context
                        rawVersion = rawVersion.replace('-SNAPSHOT', '')
                        if (env.BRANCH_NAME == 'develop') {
                            env.SERVICE_VERSION = "${rawVersion}-SNAPSHOT-${env.GIT_SHA}"
                        } else if (env.BRANCH_NAME ==~ /release\/.*/) {
                            env.SERVICE_VERSION = "${rawVersion}-rc.${env.BUILD_NUMBER}"
                        } else if (env.BRANCH_NAME == 'main') {
                            env.SERVICE_VERSION = rawVersion
                        } else if (env.BRANCH_NAME ==~ /poc\/.*/) {
                            def pocName = env.BRANCH_NAME.replace('poc/', '')
                            env.SERVICE_VERSION = "poc-${pocName}"
                        } else {
                            env.SERVICE_VERSION = rawVersion
                        }
                        echo "Building ${env.SERVICE_NAME} @ ${env.SERVICE_VERSION}"
                    }
                }
            }

            stage('Build') {
                steps {
                    script {
                        if (buildCfg?.build) {
                            container(buildCfg.build.container) {
                                sh buildCfg.build.command
                            }
                        } else if (legacyTemplate == 'springboot') {
                            container('maven') {
                                sh "mvn -B clean package -DskipTests"
                            }
                        } else if (legacyTemplate == 'react') {
                            container('node') {
                                sh "npm ci && npm run build"
                            }
                        } else if (legacyTemplate == 'python-api') {
                            container('python') {
                                sh "pip install -r requirements.txt"
                            }
                        }
                    }
                }
            }

            stage('Test') {
                steps {
                    script {
                        if (buildCfg?.test) {
                            container(buildCfg.test.container) {
                                sh buildCfg.test.command
                            }
                        } else if (legacyTemplate == 'springboot') {
                            container('maven') {
                                sh "mvn -B test"
                            }
                        } else if (legacyTemplate == 'react') {
                            container('node') {
                                sh "npm test -- --watchAll=false --passWithNoTests"
                            }
                        } else if (legacyTemplate == 'python-api') {
                            container('python') {
                                sh "pytest tests/ -v --tb=short || true"
                            }
                        }
                    }
                }
                post {
                    always {
                        script {
                            def reports = buildCfg?.test?.reports ?: '**/surefire-reports/*.xml,**/test-results/*.xml'
                            junit allowEmptyResults: true, testResults: reports
                        }
                    }
                }
            }

            stage('Quality gate') {
                when { not { branch 'poc/*' } }   // skip on POC branches
                steps {
                    script {
                        withSonarQubeEnv('sonarqube') {
                            if (buildCfg?.sonar) {
                                container(buildCfg.sonar.container) {
                                    sh buildCfg.sonar.command
                                }
                            } else if (legacyTemplate == 'springboot') {
                                container('maven') {
                                    sh "mvn sonar:sonar -Dsonar.projectKey=${env.SERVICE_NAME}"
                                }
                            } else if (legacyTemplate == 'react') {
                                container('sonar-scanner') {
                                    sh """
                                        sonar-scanner \
                                            -Dsonar.projectKey=${env.SERVICE_NAME} \
                                            -Dsonar.sources=src \
                                            -Dsonar.javascript.lcov.reportPaths=coverage/lcov.info
                                    """
                                }
                            } else if (legacyTemplate == 'python-api') {
                                container('sonar-scanner') {
                                    sh """
                                        sonar-scanner \
                                            -Dsonar.projectKey=${env.SERVICE_NAME} \
                                            -Dsonar.sources=. \
                                            -Dsonar.python.coverage.reportPaths=coverage.xml \
                                            -Dsonar.python.version=3
                                    """
                                }
                            }
                        }
                    }
                    timeout(time: 5, unit: 'MINUTES') {
                        waitForQualityGate abortPipeline: true
                    }
                }
            }

            stage('Validate dependencies') {
                steps {
                    sh """
                        python3 /opt/platform/scripts/platform_cli.py \
                            service info --name ${env.SERVICE_NAME} --json || true
                    """
                }
            }

            stage('Docker build & push') {
                steps {
                    container('docker') {
                        script {
                            def tag = "${env.REGISTRY}/${env.SERVICE_NAME}:${env.SERVICE_VERSION}"
                            def buildDate = sh(script: "date -u +%Y-%m-%dT%H:%M:%SZ", returnStdout: true).trim()
                            sh """
                                docker build -t ${tag} \
                                    --build-arg APP_VERSION=${env.SERVICE_VERSION} \
                                    --build-arg GIT_COMMIT=${env.GIT_SHA} \
                                    --build-arg BUILD_DATE=${buildDate} \
                                    .
                            """
                            sh "docker push ${tag}"
                            env.IMAGE_TAG = tag
                        }
                    }
                }
            }

            stage('Helm package & push') {
                environment {
                    HELM_REGISTRY     = credentials('helm-registry-url')
                    ARTIFACTORY_CREDS = credentials('artifactory-credentials')
                }
                steps {
                    container('docker') {
                        script {
                            sh """
                                helm package helm/ \
                                    --version ${env.SERVICE_VERSION} \
                                    --app-version ${env.SERVICE_VERSION} \
                                    --destination target/
                            """
                            // Push Helm chart to Artifactory OCI registry
                            sh """
                                echo "${env.ARTIFACTORY_CREDS_PSW}" | \
                                    helm registry login ${env.HELM_REGISTRY} \
                                        --username "${env.ARTIFACTORY_CREDS_USR}" \
                                        --password-stdin
                                helm push target/${env.SERVICE_NAME}-${env.SERVICE_VERSION}.tgz \
                                    oci://${env.HELM_REGISTRY}/helm-local
                            """
                        }
                    }
                }
            }

            stage('Release Promotion') {
                // Only on main branch: retag the SNAPSHOT image and Helm chart
                // to the release version in Artifactory — no rebuild, bit-for-bit identical.
                when { branch 'main' }
                environment {
                    ARTIFACTORY_URL   = credentials('artifactory-url')
                    ARTIFACTORY_CREDS = credentials('artifactory-credentials')
                    HELM_REGISTRY     = credentials('helm-registry-url')
                }
                steps {
                    container('python') {
                        script {
                            // Resolve the snapshot tag from the last release/* commit.
                            // The release branch was already built as X.Y.Z-rc.N; the final
                            // SNAPSHOT that was tested is X.Y.Z-SNAPSHOT-<sha> on develop.
                            // Convention: the release/* branch build produced rc tags, and
                            // the main merge carries the same version in version.txt.
                            // We promote by retagging in Artifactory via REST API.
                            def snapshotPattern = "${env.SERVICE_VERSION}-SNAPSHOT-"
                            sh """
                                python3 - <<'EOF'
import os, sys, requests

artifactory_url = os.environ['ARTIFACTORY_URL']
creds = (os.environ['ARTIFACTORY_CREDS_USR'], os.environ['ARTIFACTORY_CREDS_PSW'])
service = os.environ['SERVICE_NAME']
release_version = os.environ['SERVICE_VERSION']
snapshot_pattern = release_version + '-SNAPSHOT-'

# Find the latest snapshot image for this service in docker-local
search_url = f"{artifactory_url}/artifactory/api/search/artifact"
resp = requests.get(search_url, params={
    'name': service,
    'repos': 'docker-local',
}, auth=creds)
resp.raise_for_status()
results = resp.json().get('results', [])

# Find the snapshot tag matching our release version prefix
snapshot_tag = None
for r in results:
    uri = r.get('uri', '')
    if snapshot_pattern in uri:
        # Extract the tag from the path: docker-local/service/tag/manifest.json
        parts = uri.rstrip('/manifest.json').split('/')
        candidate_tag = parts[-1] if parts else None
        if candidate_tag and candidate_tag.startswith(snapshot_pattern):
            snapshot_tag = candidate_tag
            break

if not snapshot_tag:
    print(f"WARNING: No snapshot found matching '{snapshot_pattern}*' in docker-local.")
    print("This can happen on the first release. Skipping promotion — Docker build was used directly.")
    sys.exit(0)

print(f"Promoting {service}:{snapshot_tag} → {service}:{release_version}")

# Promote Docker image: retag snapshot → release (copy, not move)
promote_url = f"{artifactory_url}/artifactory/api/docker/docker-local/v2/promote"
payload = {
    'targetRepo':         'docker-release',
    'dockerRepository':   service,
    'tag':                snapshot_tag,
    'targetTag':          release_version,
    'copy':               True,
}
resp = requests.post(promote_url, json=payload, auth=creds)
if resp.status_code not in (200, 201):
    print(f"WARNING: Docker promotion returned {resp.status_code}: {resp.text}", file=sys.stderr)
    sys.exit(1)
print(f"Docker image promoted: {service}:{release_version} in docker-release")

# Copy Helm chart from helm-local (snapshot) to helm-release
helm_src  = f"helm-local/{service}-{snapshot_tag}.tgz"
helm_dst  = f"helm-release/{service}-{release_version}.tgz"
copy_url  = f"{artifactory_url}/artifactory/api/copy/{helm_src}?to=/{helm_dst}&failFast=0"
resp = requests.post(copy_url, auth=creds)
if resp.status_code not in (200, 201):
    print(f"WARNING: Helm chart copy returned {resp.status_code}: {resp.text}", file=sys.stderr)
    sys.exit(1)
print(f"Helm chart promoted: {service}-{release_version}.tgz in helm-release")
EOF
                            """
                        }
                    }
                }
            }

            stage('Scan deploy requests') {
                steps {
                    script {
                        // Clone the platform-config repo to scan for auto deployment requests.
                        // PLATFORM_CONFIG_REPO must be set as a Jenkins global environment variable.
                        def platformRepo = env.PLATFORM_CONFIG_REPO
                        if (!platformRepo) {
                            echo "PLATFORM_CONFIG_REPO not set — skipping deploy request scan"
                            return
                        }
                        dir('_platform_config') {
                            git url: platformRepo, credentialsId: 'github-token', shallow: true
                        }
                        def envsDir = '_platform_config/envs'
                        def versionFiles = sh(
                            script: "find '${envsDir}' -maxdepth 2 -name versions.yaml",
                            returnStdout: true
                        ).trim().split('\n').findAll { it }
                        for (versionsFile in versionFiles) {
                            def envName = versionsFile.tokenize('/')[-2]
                            def versions = readYaml(file: versionsFile)
                            def requests = versions?.requested_deployments ?: [:]
                            def svcRequest = requests[env.SERVICE_NAME]
                            if (svcRequest && svcRequest.status == 'pending' && svcRequest.auto == true) {
                                echo "Auto-deploying ${env.SERVICE_NAME}:${env.SERVICE_VERSION} to ${envName} (auto request)"
                                sh """
                                    python3 /opt/platform/scripts/platform_cli.py \
                                        deploy execute \
                                        --env ${envName} \
                                        --service ${env.SERVICE_NAME} \
                                        --version ${env.SERVICE_VERSION} \
                                        --force
                                """
                            }
                        }
                    }
                }
            }

            stage('Deploy DEV') {
                when { branch 'develop' }
                steps {
                    deployService(env: 'dev', service: env.SERVICE_NAME, version: env.SERVICE_VERSION)
                }
            }

            stage('Deploy STAGING') {
                when { branch ~/release\/.*/ }
                steps {
                    deployService(env: 'staging', service: env.SERVICE_NAME, version: env.SERVICE_VERSION)
                }
            }

            stage('Tag & release notes') {
                when { branch 'main' }
                steps {
                    script {
                        sh "git tag v${env.SERVICE_VERSION}"
                        sh "git push origin v${env.SERVICE_VERSION}"
                        generateReleaseNotes(service: env.SERVICE_NAME, version: env.SERVICE_VERSION)
                    }
                }
            }

            stage('Deploy PROD') {
                when { branch 'main' }
                steps {
                    timeout(time: 24, unit: 'HOURS') {
                        input(
                            message: "Deploy ${env.SERVICE_NAME} ${env.SERVICE_VERSION} to PROD?",
                            ok: "Deploy",
                            submitter: "${env.PROD_APPROVERS ?: 'devops-team'}",
                        )
                    }
                    deployService(env: 'prod', service: env.SERVICE_NAME, version: env.SERVICE_VERSION)
                }
            }
        }

        post {
            success { echo "Build succeeded: ${env.SERVICE_NAME} @ ${env.SERVICE_VERSION}" }
            failure { echo "Build failed: ${env.SERVICE_NAME} @ ${env.SERVICE_VERSION}" }
        }
    }
}
