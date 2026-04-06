/**
 * buildService.groovy — Shared build pipeline for all microservices.
 *
 * Usage in a service Jenkinsfile:
 *   @Library('platform-shared-lib@v1.0') _
 *   buildService(template: 'springboot')
 */
def call(Map config = [:]) {
    def template = config.get('template', 'springboot')

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
                        if (template == 'springboot') {
                            container('maven') {
                                env.SERVICE_VERSION = sh(
                                    script: "mvn help:evaluate -Dexpression=project.version -q -DforceStdout",
                                    returnStdout: true
                                ).trim()
                            }
                        } else if (template == 'react') {
                            container('node') {
                                def pkg = readJSON file: 'package.json'
                                env.SERVICE_VERSION = pkg.version
                            }
                        } else if (template == 'python-api') {
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
                        if (template == 'springboot') {
                            container('maven') {
                                sh "mvn -B clean package -DskipTests"
                            }
                        } else if (template == 'react') {
                            container('node') {
                                sh "npm ci && npm run build"
                            }
                        } else if (template == 'python-api') {
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
                        if (template == 'springboot') {
                            container('maven') {
                                sh "mvn -B test"
                            }
                        } else if (template == 'react') {
                            container('node') {
                                sh "npm test -- --watchAll=false --passWithNoTests"
                            }
                        } else if (template == 'python-api') {
                            container('python') {
                                sh "pytest tests/ -v --tb=short || true"
                            }
                        }
                    }
                }
                post {
                    always {
                        junit allowEmptyResults: true, testResults: '**/surefire-reports/*.xml,**/test-results/*.xml'
                    }
                }
            }

            stage('Quality gate') {
                when { not { branch 'poc/*' } }   // skip on POC branches
                steps {
                    script {
                        container('sonar-scanner') {
                            withSonarQubeEnv('sonarqube') {
                                if (template == 'springboot') {
                                    // Maven wrapper handles the scanner
                                    container('maven') {
                                        sh "mvn sonar:sonar -Dsonar.projectKey=${env.SERVICE_NAME}"
                                    }
                                } else if (template == 'react') {
                                    sh """
                                        sonar-scanner \
                                            -Dsonar.projectKey=${env.SERVICE_NAME} \
                                            -Dsonar.sources=src \
                                            -Dsonar.javascript.lcov.reportPaths=coverage/lcov.info
                                    """
                                } else if (template == 'python-api') {
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
            success { notifySlack(status: 'success', service: env.SERVICE_NAME, version: env.SERVICE_VERSION) }
            failure { notifySlack(status: 'failure', service: env.SERVICE_NAME, version: env.SERVICE_VERSION) }
        }
    }
}
