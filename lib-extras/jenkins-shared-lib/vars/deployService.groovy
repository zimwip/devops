/**
 * deployService.groovy — Deploy a service version to a target environment.
 *
 * Usage:
 *   deployService(env: 'dev', service: 'service-auth', version: '2.3.0')
 */
def call(Map config) {
    def targetEnv = config.env
    def service   = config.service
    def version   = config.version

    echo "Deploying ${service}:${version} → ${targetEnv}"

    // 1. Validate inter-service dependencies
    validateDependencies(env: targetEnv, service: service)

    // 2. Helm deploy
    def namespace = "platform-${targetEnv}"
    def helmDir   = "helm/"
    sh """
        helm upgrade --install ${service} ${helmDir} \
            --namespace ${namespace} \
            --create-namespace \
            --set image.tag=${version} \
            --set env=${targetEnv} \
            --values helm/values-${targetEnv}.yaml \
            --atomic \
            --timeout 5m \
            --history-max 5
    """

    // 3. Smoke test
    sh """
        sleep 10
        kubectl rollout status deployment/${service} -n ${namespace} --timeout=3m
    """

    // 4. Update platform-config versions.yaml
    sh """
        python3 /opt/platform/scripts/platform_cli.py \
            deploy --env ${targetEnv} --service ${service} --version ${version}
    """

    echo "✓ ${service}:${version} deployed to ${targetEnv}"
}
