import chalk from 'chalk';

const formateTag = (tag: string) => {
  return `[${new Date().toISOString()}] [${tag}]`;
};
export const logger = {
  debug: (tag: string, message?: unknown, ...optionalParams: unknown[]) => {
    console.debug(chalk.blue(formateTag(tag)), message, ...optionalParams);
  },
  info: (tag: string, message?: unknown, ...optionalParams: unknown[]) => {
    console.info(chalk.green(formateTag(tag)), message, ...optionalParams);
  },
  warn: (tag: string, message?: unknown, ...optionalParams: unknown[]) => {
    console.warn(chalk.yellow(formateTag(tag)), message, ...optionalParams);
  },

  error: (tag: string, message?: unknown, ...optionalParams: unknown[]) => {
    console.error(chalk.red(formateTag(tag)), message, ...optionalParams);
  },
};
