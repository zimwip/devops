/**
 * deployService.groovy — Deploy a service version to a target environment.
 *
 * Pulls the Helm chart from the Artifactory OCI registry (helm-local for
 * pre-release versions, helm-release for production releases) instead of
 * reading a local helm/ directory.  This ensures the exact chart that was
 * packaged and tested is what lands on the cluster.
 *
 * Usage:
 *   deployService(env: 'dev', service: 'service-auth', version: '2.3.0-SNAPSHOT-a3f1c2d')
 *   deployService(env: 'prod', service: 'service-auth', version: '2.3.0')
 */
def call(Map config) {
    def targetEnv = config.env
    def service   = config.service
    def version   = config.version

    echo "Deploying ${service}:${version} → ${targetEnv}"

    // 1. Validate inter-service dependencies
    validateDependencies(env: targetEnv, service: service)

    // 2. Determine which Helm repo to pull from:
    //    - Release versions (X.Y.Z) come from helm-release (immutable)
    //    - Everything else (SNAPSHOT, rc, poc) comes from helm-local
    def isRelease = version ==~ /^\d+\.\d+\.\d+$/
    def helmRepo  = isRelease ? 'helm-release' : 'helm-local'

    def namespace = "${targetEnv}-${service}"

    // 3. Helm deploy from OCI registry
    container('docker') {
        withCredentials([
            string(credentialsId: 'artifactory-url',           variable: 'ARTIFACTORY_URL'),
            usernamePassword(credentialsId: 'artifactory-credentials',
                             usernameVariable: 'ARTIFACTORY_USER',
                             passwordVariable: 'ARTIFACTORY_PASS'),
        ]) {
            sh """
                echo "\${ARTIFACTORY_PASS}" | \\
                    helm registry login \${HELM_REGISTRY} \\
                        --username "\${ARTIFACTORY_USER}" \\
                        --password-stdin

                helm upgrade --install ${service} \\
                    oci://\${HELM_REGISTRY}/${helmRepo}/${service} \\
                    --version ${version} \\
                    --namespace ${namespace} \\
                    --create-namespace \\
                    --set image.tag=${version} \\
                    --set env=${targetEnv} \\
                    --atomic \\
                    --timeout 5m \\
                    --history-max 5
            """
        }
    }

    // 4. Smoke test — wait for rollout
    sh """
        sleep 10
        kubectl rollout status deployment/${service} -n ${namespace} --timeout=3m
    """

    // 5. Update platform-config state (version.yaml for the service in this env)
    sh """
        python3 /opt/platform/scripts/platform_cli.py \\
            deploy --env ${targetEnv} --service ${service} --version ${version}
    """

    echo "✓ ${service}:${version} deployed to ${targetEnv} (ns: ${namespace})"
}
