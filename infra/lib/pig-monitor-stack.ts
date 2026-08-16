import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecsPatterns from "aws-cdk-lib/aws-ecs-patterns";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import { Construct } from "constructs";
import { ECR_REPOSITORY_NAME } from "./ecr-stack";

export class PigMonitorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Image tag to deploy. Push the image to ECR under this tag before
    // running `cdk deploy` on this stack -- see infra/README for the
    // build/push/deploy sequence.
    const imageTag = this.node.tryGetContext("imageTag") ?? "latest";

    // No NAT Gateway: every subnet is public. The Fargate task gets a
    // public IP for outbound internet (pulling the image, calling the
    // Crusoe API); inbound is locked down by security groups, not subnet
    // isolation.
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    const repository = ecr.Repository.fromRepositoryName(
      this,
      "Repository",
      ECR_REPOSITORY_NAME,
    );

    const database = new rds.DatabaseInstance(this, "Database", {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16,
      }),
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.BURSTABLE4_GRAVITON,
        ec2.InstanceSize.MICRO,
      ),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      databaseName: "pigmonitor",
      credentials: rds.Credentials.fromGeneratedSecret("pigmonitor", {
        secretName: "pig-monitor/db-credentials",
      }),
      allocatedStorage: 20,
      storageType: rds.StorageType.GP3,
      storageEncrypted: true,
      multiAz: false,
      publiclyAccessible: false,
      backupRetention: cdk.Duration.days(1),
      enablePerformanceInsights: false,
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc,
      containerInsights: false,
    });

    const logGroup = new logs.LogGroup(this, "AppLogGroup", {
      logGroupName: "/ecs/pig-monitor",
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDefinition = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    taskDefinition.addContainer("AppContainer", {
      image: ecs.ContainerImage.fromEcrRepository(repository, imageTag),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "pig-monitor",
        logGroup,
      }),
      portMappings: [{ containerPort: 8000 }],
      environment: {
        PIGWATCH_ENCLOSURE_ID: "01",
      },
      secrets: {
        DB_HOST: ecs.Secret.fromSecretsManager(database.secret!, "host"),
        DB_PORT: ecs.Secret.fromSecretsManager(database.secret!, "port"),
        DB_NAME: ecs.Secret.fromSecretsManager(database.secret!, "dbname"),
        DB_USER: ecs.Secret.fromSecretsManager(database.secret!, "username"),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(
          database.secret!,
          "password",
        ),
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "python -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)\" || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(10),
      },
    });

    // ALB + Fargate service, 1 task, public subnets, public IP. The pattern
    // wires an ALB security group open on :80 to 0.0.0.0/0 and a service
    // security group that only accepts traffic from the ALB -- the task's
    // public IP is not otherwise reachable.
    const service = new ecsPatterns.ApplicationLoadBalancedFargateService(
      this,
      "Service",
      {
        cluster,
        taskDefinition,
        desiredCount: 1,
        publicLoadBalancer: true,
        listenerPort: 80,
        assignPublicIp: true,
        taskSubnets: { subnetType: ec2.SubnetType.PUBLIC },
        healthCheckGracePeriod: cdk.Duration.seconds(60),
        minHealthyPercent: 0,
        circuitBreaker: { rollback: true },
      },
    );

    service.targetGroup.configureHealthCheck({
      path: "/health",
      healthyHttpCodes: "200",
    });

    database.connections.allowDefaultPortFrom(
      service.service,
      "Allow the Fargate task to reach Postgres",
    );

    new cdk.CfnOutput(this, "LoadBalancerDnsName", {
      value: service.loadBalancer.loadBalancerDnsName,
    });

    new cdk.CfnOutput(this, "DatabaseSecretArn", {
      value: database.secret!.secretArn,
    });

    new cdk.CfnOutput(this, "DatabaseEndpoint", {
      value: database.instanceEndpoint.hostname,
    });
  }
}
