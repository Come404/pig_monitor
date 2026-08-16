import * as cdk from "aws-cdk-lib";
import * as ecr from "aws-cdk-lib/aws-ecr";
import { Construct } from "constructs";

export const ECR_REPOSITORY_NAME = "pig-monitor";

export class PigMonitorEcrStack extends cdk.Stack {
  public readonly repository: ecr.Repository;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.repository = new ecr.Repository(this, "Repository", {
      repositoryName: ECR_REPOSITORY_NAME,
      imageScanOnPush: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [
        {
          description: "Keep only the last 5 images to bound storage cost",
          maxImageCount: 5,
        },
      ],
    });

    new cdk.CfnOutput(this, "RepositoryUri", {
      value: this.repository.repositoryUri,
    });
  }
}
