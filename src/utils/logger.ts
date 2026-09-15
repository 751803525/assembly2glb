import chalk from 'chalk';

const formateTag = (tag: string) => {
  return `[${tag} - ${new Date().toISOString()}]`;
};
export const logger = {
  debug: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.debug(chalk.blue(formateTag(tag)), message, ...optionalParams);
  },
  info: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.info(chalk.green(formateTag(tag)), message, ...optionalParams);
  },
  warn: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.warn(chalk.yellow(formateTag(tag)), message, ...optionalParams);
  },

  error: (tag: string, message?: any, ...optionalParams: any[]) => {
    console.error(chalk.red(formateTag(tag)), message, ...optionalParams);
  },
};
