AWS_PROFILE  ?= NWCI-Prod
AWS_REGION   ?= ap-southeast-2
IMAGE_NAME   ?= jobpac-export
ECR_URL      := $(shell AWS_PROFILE=$(AWS_PROFILE) aws ecr describe-repositories \
                  --repository-names $(IMAGE_NAME) \
                  --region $(AWS_REGION) \
                  --query "repositories[0].repositoryUri" \
                  --output text 2>/dev/null)

.PHONY: build build-local push deploy run run-local logs

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

run:
	AWS_PROFILE=$(AWS_PROFILE) aws ecs run-task \
	  --cluster $(IMAGE_NAME) \
	  --task-definition $(IMAGE_NAME) \
	  --launch-type FARGATE \
	  --network-configuration "awsvpcConfiguration={subnets=[subnet-03f1ed90208bf3cc1,subnet-0621560e675973ca9],securityGroups=[sg-04ad480df46a87f0e],assignPublicIp=DISABLED}" \
	  --region $(AWS_REGION)

run-local: build-local
	docker run --rm \
	  -v ~/.aws:/root/.aws:ro \
	  -e AWS_PROFILE=NWCI-Prod \
	  -e JOBPAC_SECRET_NAME=prod/jobpac/db2 \
	  -e S3_BUCKET=nwci-pbi-bucket \
	  -e S3_PREFIX=jobpac-api/test/ \
	  -e NOTIFICATION_RECIPIENTS=tai@aidatapros.com \
	  $(IMAGE_NAME):local

logs:
	AWS_PROFILE=$(AWS_PROFILE) aws logs tail /ecs/$(IMAGE_NAME) --follow --region $(AWS_REGION)
