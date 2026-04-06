FROM jenkins/jenkins:lts-jdk17

# Pre-install plugins so the container starts fast (no download on first boot).
# Build with: docker compose build jenkins
COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Skip the manual setup wizard — JCasC handles all configuration.
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
ENV CASC_JENKINS_CONFIG="/var/jenkins_home/casc_configs"
