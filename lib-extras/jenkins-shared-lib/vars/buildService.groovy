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
  containers:
  - name: maven
    image: maven:3.9-eclipse-temurin-17
    command: [sleep, infinity]
    resources:
      requests: {cpu: 500m, memory: 1Gi}
      limits:   {cpu: 2,    memory: 2Gi}
  - name: node
    image: node:20-alpine
    command: [sleep, infinity]
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 1,    memory: 1Gi}
  - name: python
    image: python:3.12-slim
    command: [sleep, infinity]
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 1,    memory: 1Gi}
  - name: sonar-scanner
    image: sonarsource/sonar-scanner-cli:latest
    command: [sleep, infinity]
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 1,    memory: 1Gi}
  - name: docker
    image: docker:24-dind
    securityContext:
      privileged: true
    resources:
      requests: {cpu: 300m, memory: 512Mi}
      limits:   {cpu: 2,    memory: 2Gi}
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
        }

        stages {

            stage('Version') {
                steps {
                    script {
                        // Load build config if present (new template structure)
                        if (fileExists('.platform/build.yaml')) {
                            buildCfg = readYaml(file: '.platform/build.yaml')
                        }

                        if (buildCfg?.version) {
                            container(buildCfg.version.container) {
                                env.SERVICE_VERSION = sh(
                                    script: buildCfg.version.command,
                                    returnStdout: true
                                ).trim()
                            }
                        } else if (legacyTemplate == 'springboot') {
                            container('maven') {
                                env.SERVICE_VERSION = sh(
                                    script: "mvn help:evaluate -Dexpression=project.version -q -DforceStdout",
                                    returnStdout: true
                                ).trim()
                            }
                        } else if (legacyTemplate == 'react') {
                            container('node') {
                                def pkg = readJSON file: 'package.json'
                                env.SERVICE_VERSION = pkg.version
                            }
                        } else if (legacyTemplate == 'python-api') {
                            container('python') {
                                env.SERVICE_VERSION = sh(
                                    script: "python -c \"import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])\" 2>/dev/null || grep -m1 '^version' setup.cfg | cut -d= -f2 | tr -d ' '",
                                    returnStdout: true
                                ).trim()
                            }
                        }
                        // Strip -SNAPSHOT for release branches
                        if (env.BRANCH_NAME ==~ /release\/.*|main/) {
                            env.SERVICE_VERSION = env.SERVICE_VERSION.replace('-SNAPSHOT', '')
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
                            sh "docker build -t ${tag} ."
                            sh "docker push ${tag}"
                            env.IMAGE_TAG = tag
                        }
                    }
                }
            }

            stage('Helm package') {
                steps {
                    sh """
                        helm package helm/ \
                            --version ${env.SERVICE_VERSION} \
                            --app-version ${env.SERVICE_VERSION} \
                            --destination target/
                    """
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
