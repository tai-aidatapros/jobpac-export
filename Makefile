AWS_PROFILE  ?= NWCI-Prod
AWS_REGION   ?= ap-southeast-2
IMAGE_NAME   ?= jobpac-export
MAX_WORKERS  ?=
ECR_URL      := $(shell AWS_PROFILE=$(AWS_PROFILE) aws ecr describe-repositories \
                  --repository-names $(IMAGE_NAME) \
                  --region $(AWS_REGION) \
                  --query "repositories[0].repositoryUri" \
                  --output text 2>/dev/null)

# Resolved from Terraform state so they always match what was deployed
TF_SUBNET    := $(shell cd infra && AWS_PROFILE=$(AWS_PROFILE) terraform output -raw public_subnet_id 2>/dev/null)
TF_SG        := $(shell cd infra && AWS_PROFILE=$(AWS_PROFILE) terraform output -raw task_security_group_id 2>/dev/null)
SUBNET       := $(or $(TF_SUBNET),subnet-05a0fc40e439cffd9)
SG           := $(or $(TF_SG),sg-04ad480df46a87f0e)

.PHONY: build build-local push deploy run run-local run-native logs check-connectivity test

test:
	python3 -m pytest tests/

check-connectivity:
	python3 -m src.connectivity_check

build:
	docker build --platform linux/amd64 -t $(IMAGE_NAME):latest .

build-local:
	docker build -t $(IMAGE_NAME):local .

push: build
	AWS_PROFILE=$(AWS_PROFILE) aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_URL)
	docker tag $(IMAGE_NAME):latest $(ECR_URL):latest
	docker push $(ECR_URL):latest

deploy:
	cd infra && AWS_PROFILE=$(AWS_PROFILE) terraform apply -auto-approve

# Build the --overrides JSON only when MAX_WORKERS is set
_OVERRIDES = $(if $(MAX_WORKERS),--overrides '{"containerOverrides":[{"name":"$(IMAGE_NAME)","command":["python","-m","src.main","--max-workers","$(MAX_WORKERS)"]}]}',)

run:
	AWS_PROFILE=$(AWS_PROFILE) aws ecs run-task \
	  --cluster $(IMAGE_NAME) \
	  --task-definition $(IMAGE_NAME) \
	  --launch-type FARGATE \
	  --network-configuration "awsvpcConfiguration={subnets=[$(SUBNET)],securityGroups=[$(SG)],assignPublicIp=ENABLED}" \
	  --region $(AWS_REGION) \
	  $(_OVERRIDES) \
	  --no-cli-pager || (echo "ERROR: aws ecs run-task failed — check output above" && exit 1)
	@echo "Task submitted — tailing logs (Ctrl+C to stop)..."
	@sleep 5
	$(MAKE) logs

run-local: build-local
	docker run --rm \
	  -v ~/.aws:/root/.aws:ro \
	  --add-host=host.docker.internal:host-gateway \
	  -e AWS_PROFILE=NWCI-Prod \
	  -e JOBPAC_SECRET_NAME=prod/jobpac/db2 \
	  -e S3_BUCKET=nwci-pbi-bucket \
	  -e S3_PREFIX=jobpac-api/test/ \
	  -e NOTIFICATION_RECIPIENTS=tai@aidatapros.com \
	  $(IMAGE_NAME):local

run-native:
	bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python3 src/main.py $(if $(MAX_WORKERS),--max-workers $(MAX_WORKERS),)'

run-tables:
	@test -n "$(TABLES)" || (echo "Usage: make run-tables TABLES=TABLE1,TABLE2" && exit 1)
	bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python3 src/main.py --tables "$(TABLES)" $(if $(MAX_WORKERS),--max-workers $(MAX_WORKERS),)'

diagnose:
	bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python3 scripts/diagnose_table.py $(TABLE)'

diagnose-all:
	bash -c 'set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python3 scripts/diagnose_table.py --all'

logs:
	AWS_PROFILE=$(AWS_PROFILE) aws logs tail /ecs/$(IMAGE_NAME) --follow --region $(AWS_REGION)
