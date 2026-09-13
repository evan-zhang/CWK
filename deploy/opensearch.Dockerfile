FROM opensearchproject/opensearch:2.15.0

# Install at image build time so runtime startup does not depend on network access.
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu
